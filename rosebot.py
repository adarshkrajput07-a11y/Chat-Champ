"""
Bot name - 𝑴𝑨𝑻𝑹𝑰𝑿 𝑪𝑯𝑨𝑻 𝑩𝑶𝑻 💬🍃

Premium-style group management bot (Miss Rose style).
Setup:  pip install -r requirements.txt
Run:    python rosebot.py   (token + owner id come from config.py or env vars)
"""
import asyncio
import html
import logging
import os
import re
import sqlite3
import time
import unicodedata
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps
from types import SimpleNamespace

from deep_translator import GoogleTranslator
from telegram import (
    ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update, User,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application, ApplicationHandlerStop, CallbackQueryHandler, ChatMemberHandler,
    CommandHandler, ContextTypes, MessageHandler, filters,
)

# Token and owner id live in config.py (kept out of GitHub).
# Falls back to BOT_TOKEN / OWNER_ID environment variables.
try:
    from config import BOT_TOKEN, OWNER_ID
except ImportError:
    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    OWNER_ID = int(os.environ.get("OWNER_ID", "0"))

logging.basicConfig(level=logging.INFO)

BOT_NAME = "𝑴𝑨𝑻𝑹𝑰𝑿 𝑪𝑯𝑨𝑻 𝑩𝑶𝑻 💬🍃"

# ---------------------------------------------------------------- style
_SC = dict(zip("abcdefghijklmnopqrstuvwxyz", "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
esc = html.escape

OK, BLUE, RED = "success", "primary", "danger"  # button colours: green / blue / red


def sc(text: str) -> str:
    """ʙᴏᴡʟᴇʀ-style small caps."""
    return "".join(_SC.get(c.lower(), c) for c in text)


def L(label, value):
    return f"▫️ <b>{sc(label)}</b> ➜ {value}"


def card(emoji, title, *lines):
    head = f"{emoji} <b>{sc(title)}</b>"
    return head + ("\n\n" + "\n".join(lines) if lines else "")


def cmd(c, desc):
    return f"▫️ <code>{c}</code> ➜ {sc(desc)}"


def btn(emoji, label, data=None, url=None, style=None):
    return InlineKeyboardButton(f"{emoji} {sc(label)}", callback_data=data,
                                url=url, style=style)


def kb(*rows):
    return InlineKeyboardMarkup(list(rows))


async def say(update: Update, text, markup=None):
    return await update.effective_message.reply_text(
        text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def usage(update, text):
    await say(update, card("ℹ️", "usage", f"<code>{esc(text)}</code>"))


# ---------------------------------------------------------------- database
db = sqlite3.connect(os.environ.get("DB_PATH", "rosebot.db"), check_same_thread=False)
db.executescript("""
CREATE TABLE IF NOT EXISTS settings(
    chat_id INTEGER PRIMARY KEY, welcome TEXT, welcome_on INTEGER DEFAULT 1,
    rules TEXT, warn_limit INTEGER DEFAULT 3, flood INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS notes(chat_id INTEGER, name TEXT, text TEXT,
    PRIMARY KEY(chat_id, name));
CREATE TABLE IF NOT EXISTS filters(chat_id INTEGER, kw TEXT, reply TEXT,
    PRIMARY KEY(chat_id, kw));
CREATE TABLE IF NOT EXISTS warns(chat_id INTEGER, user_id INTEGER, n INTEGER,
    PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS members(chat_id INTEGER, user_id INTEGER,
    PRIMARY KEY(chat_id, user_id));
CREATE TABLE IF NOT EXISTS chats(chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT);
CREATE TABLE IF NOT EXISTS bot_users(user_id INTEGER PRIMARY KEY, name TEXT);
CREATE TABLE IF NOT EXISTS fbans(user_id INTEGER PRIMARY KEY, name TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS tags(chat_id INTEGER, username TEXT, tag TEXT,
    user_id INTEGER, applied INTEGER DEFAULT 0, PRIMARY KEY(chat_id, username));
CREATE TABLE IF NOT EXISTS locks(chat_id INTEGER, kind TEXT, PRIMARY KEY(chat_id, kind));
""")


for _ddl in ("ALTER TABLE settings ADD COLUMN welcome_media TEXT",
             "ALTER TABLE settings ADD COLUMN welcome_file TEXT",
             "ALTER TABLE members ADD COLUMN username TEXT",
             "ALTER TABLE members ADD COLUMN name TEXT"):
    try:
        db.execute(_ddl)
    except sqlite3.OperationalError:
        pass  # column already exists
db.commit()


def get_setting(chat_id, col):
    db.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
    return db.execute(f"SELECT {col} FROM settings WHERE chat_id=?", (chat_id,)).fetchone()[0]


def set_setting(chat_id, col, val):
    db.execute("INSERT OR IGNORE INTO settings(chat_id) VALUES(?)", (chat_id,))
    db.execute(f"UPDATE settings SET {col}=? WHERE chat_id=?", (val, chat_id))
    db.commit()


# ---------------------------------------------------------------- helpers
FULL = ChatPermissions(can_send_messages=True, can_send_polls=True,
                       can_send_other_messages=True, can_add_web_page_previews=True,
                       can_invite_users=True)
MUTED = ChatPermissions(can_send_messages=False)


async def is_admin(chat, user_id):
    if user_id == OWNER_ID:  # the bot owner can use every admin command
        return True
    m = await chat.get_member(user_id)
    return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


def admin_only(fn):
    @wraps(fn)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        chat, user = update.effective_chat, update.effective_user
        if chat.type == "private":
            return await say(update, card("👥", "groups only", sc("use this command in a group")))
        if not await is_admin(chat, user.id):
            return await say(update, card("🔒", "admins only", sc("you can't use this command")))
        return await fn(update, ctx)
    return wrapper


async def resolve_target(update: Update, ctx):
    """Find the target user: reply, text-mention, @username or numeric id."""
    msg, chat = update.message, update.effective_chat
    if msg.reply_to_message:
        return msg.reply_to_message.from_user
    for ent in msg.entities or []:
        if ent.type == "text_mention" and ent.user:
            return ent.user
    for arg in ctx.args or []:
        if arg.startswith("@") and len(arg) > 1:
            uname = arg[1:].lower()
            row = db.execute(
                "SELECT user_id, username, name FROM members "
                "WHERE chat_id=? AND lower(username)=?", (chat.id, uname)).fetchone()
            if row:
                return User(id=row[0], first_name=row[2] or row[1] or str(row[0]),
                            is_bot=False, username=row[1])
            for m in await chat.get_administrators():
                if m.user.username and m.user.username.lower() == uname:
                    return m.user
            return None
        if arg.isdigit() and len(arg) >= 6:
            return User(id=int(arg), first_name=arg, is_bot=False)
    return None


async def need_target(update: Update, ctx):
    t = await resolve_target(update, ctx)
    if not t:
        await say(update, card(
            "🔎", "user not found",
            sc("reply to the user, or use their @username / id"),
            f"<i>{sc('i can only find usernames of people i have seen in this group')}</i>"))
    return t


async def is_protected(chat, ctx, user_id):
    """Admins, the bot owner and the bot itself can't be punished."""
    if user_id in (OWNER_ID, ctx.bot.id):
        return True
    try:
        m = await chat.get_member(user_id)
    except Exception:
        return False
    return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


PROTECTED = card("🛡", "protected", sc("admins can't be banned, kicked, muted or warned"))


def user_link(uid):
    return f'<a href="tg://user?id={uid}">{uid}</a>'


# ---------------------------------------------------------------- start / help
PAGES = {
    "mod": ("🛡", "moderation", [
        cmd("/ban", "ban a user (reply)"),
        cmd("/unban", "unban a user (reply)"),
        cmd("/kick", "remove a user, they can rejoin"),
        cmd("/mute", "stop a user from talking"),
        cmd("/unmute", "let a user talk again"),
        "▫️ " + sc("target: reply to a user, or use @username / id"),
    ]),
    "warn": ("⚠️", "warnings", [
        cmd("/warn", "warn a user (reply)"),
        cmd("/warns", "check warnings"),
        cmd("/resetwarns", "clear a user's warnings"),
        cmd("/setwarnlimit N", "warnings before auto-ban"),
    ]),
    "welcome": ("👋", "welcome & rules", [
        cmd("/setwelcome", "reply to any message to use it as welcome"),
        "▫️ <code>/setwelcome text</code> ➜ " + sc("set a text welcome"),
        "▫️ " + sc("use") + " <code>{Username}</code> <code>{name}</code> <code>{group}</code>",
        cmd("/welcome", "preview the welcome message"),
        cmd("/welcome on|off", "toggle welcome message"),
        cmd("/resetwelcome", "back to the default welcome"),
        cmd("/setrules text", "set group rules"),
        cmd("/rules", "show group rules"),
    ]),
    "notes": ("📝", "notes", [
        cmd("/save name text", "save a note (or reply)"),
        cmd("/get name", "show a note, or type #name"),
        cmd("/notes", "list all notes"),
        cmd("/clear name", "delete a note"),
    ]),
    "filters": ("🔎", "filters", [
        cmd("/filter word reply", "auto-reply to a word"),
        cmd("/stop word", "remove a filter"),
        cmd("/filters", "list all filters"),
    ]),
    "admin": ("👑", "admins", [
        cmd("/purge", "delete messages from the replied one down"),
        cmd("/promote", "make admin with basic rights"),
        cmd("/promote all", "make admin with all rights"),
        cmd("/demote", "remove admin rights"),
        cmd("/adminlist", "show all admins"),
        cmd("/lock type", "lock a message type for non-admins"),
        cmd("/unlock type", "unlock a message type"),
        cmd("/locks", "show what is locked"),
        "▫️ " + sc("types:") + " <code>all message stickers gif photo video voice link ...</code>",
        cmd("/tagall", "tag all members, 5 per message"),
        cmd("/tagall text", "tag everyone with a message"),
        cmd("/stoptag", "stop tagging"),
        "▫️ " + sc("target: reply to a user, or use @username / id"),
    ]),
    "owner": ("🌐", "owner tools", [
        cmd("/broadcast text", "send to all groups and users"),
        cmd("/broadcast -groups text", "groups only (or -users)"),
        cmd("/fban", "ban a user from every group i am in"),
        cmd("/unfban", "lift a federation ban"),
        "▫️ <code>/addtag groupid</code> ➜ " + sc("give players their tags"),
        "▫️ " + sc("then one per line:") + " <code>@username - Tag</code>",
    ]),
    "music": ("🎵", "music & video", [
        cmd("/play name", "play a song in the voice chat"),
        cmd("/vplay name", "play a song with video"),
        cmd("/pause", "pause the music"),
        cmd("/resume", "resume the music"),
        cmd("/skip", "skip to the next song"),
        cmd("/end", "stop and leave the voice chat"),
        cmd("/queue", "show the song queue"),
        cmd("/autoplay", "toggle autoplay of related songs"),
        "▫️ " + sc("start a voice chat first, then use /play"),
    ]),
    "tools": ("🧰", "tools", [
        cmd("/setflood N", "mute spammers, N msgs in 5s (0 = off)"),
        cmd("/id", "reply to a user for their id, or get the group id"),
        cmd("/kickme", "leave the group (non-admins)"),
        cmd("/tr [lang]", "translate a replied message"),
        cmd("/tr lang text", "translate typed text"),
        cmd("/zombies", "scan for deleted accounts"),
        cmd("/zombies clean", "remove deleted accounts"),
    ]),
}


def help_main_text():
    return card("📚", "help menu", sc("pick a category below ") + "👇")


def help_menu():
    return kb(
        [btn("🛡", "moderation", "help:mod", style=RED),
         btn("⚠️", "warnings", "help:warn", style=BLUE)],
        [btn("👋", "welcome", "help:welcome", style=OK),
         btn("📝", "notes", "help:notes", style=BLUE)],
        [btn("🔎", "filters", "help:filters", style=OK),
         btn("🧰", "tools", "help:tools", style=RED)],
        [btn("👑", "admins", "help:admin", style=OK),
         btn("🌐", "owner", "help:owner", style=BLUE)],
        [btn("🎵", "music", "help:music", style=OK),
         btn("✖️", "close", "close", style=RED)],
    )


def page_menu():
    return kb([btn("⬅️", "back", "help:main", style=BLUE),
               btn("✖️", "close", "close", style=RED)])


async def start(update: Update, ctx):
    if update.effective_chat.type != "private":
        return await say(update, card(
            "💬", "i'm alive!", f"{sc('send')} /help {sc('to see what i can do')} ✨"))
    add_url = (f"https://t.me/{ctx.bot.username}?startgroup=true"
               "&admin=delete_messages+restrict_members+invite_users+pin_messages")
    rows = [[btn("➕", "add me to your group", url=add_url, style=OK)],
            [btn("📚", "help", "help:main", style=BLUE)]]
    if OWNER_ID:
        rows[1].append(btn("👑", "owner", url=f"tg://user?id={OWNER_ID}", style=BLUE))
    rows.append([btn("✖️", "close", "close", style=RED)])
    text = (f"✨ <b>{BOT_NAME}</b> ✨\n\n"
            f"{sc('i keep your groups clean, safe and fun')} 🍃\n\n"
            f"🛡 {sc('moderation & warnings')}\n"
            f"👋 {sc('welcome messages & rules')}\n"
            f"📝 {sc('notes & filters')}\n"
            f"🌐 {sc('translate messages')}\n"
            f"🌊 {sc('antiflood protection')}")
    await say(update, text, kb(*rows))


async def help_cmd(update: Update, ctx):
    await say(update, help_main_text(), help_menu())


# ---------------------------------------------------------------- moderation
@admin_only
async def ban(update, ctx):
    t = await need_target(update, ctx)
    if not t:
        return
    if await is_protected(update.effective_chat, ctx, t.id):
        return await say(update, PROTECTED)
    await update.effective_chat.ban_member(t.id)
    await say(update, card("🔨", "user banned", L("user", t.mention_html()),
                           L("by", update.effective_user.mention_html())),
              kb([btn("↩️", "unban", f"unban:{t.id}", style=OK)]))


@admin_only
async def unban(update, ctx):
    t = await need_target(update, ctx)
    if not t:
        return
    await update.effective_chat.unban_member(t.id, only_if_banned=True)
    await say(update, card("✅", "user unbanned", L("user", t.mention_html())))


@admin_only
async def kick(update, ctx):
    t = await need_target(update, ctx)
    if not t:
        return
    if await is_protected(update.effective_chat, ctx, t.id):
        return await say(update, PROTECTED)
    await update.effective_chat.ban_member(t.id)
    await update.effective_chat.unban_member(t.id)
    await say(update, card("👢", "user kicked", L("user", t.mention_html()),
                           L("by", update.effective_user.mention_html())))


@admin_only
async def mute(update, ctx):
    t = await need_target(update, ctx)
    if not t:
        return
    if await is_protected(update.effective_chat, ctx, t.id):
        return await say(update, PROTECTED)
    await update.effective_chat.restrict_member(t.id, MUTED)
    await say(update, card("🔇", "user muted", L("user", t.mention_html()),
                           L("by", update.effective_user.mention_html())),
              kb([btn("🔊", "unmute", f"unmute:{t.id}", style=OK)]))


@admin_only
async def unmute(update, ctx):
    t = await need_target(update, ctx)
    if not t:
        return
    await update.effective_chat.restrict_member(t.id, FULL)
    await say(update, card("🔊", "user unmuted", L("user", t.mention_html())))


# ---------------------------------------------------------------- purge / admins
BASIC_RIGHTS = ("can_delete_messages", "can_restrict_members",
                "can_pin_messages", "can_invite_users")
ALL_RIGHTS = BASIC_RIGHTS + ("can_change_info", "can_manage_video_chats",
                             "can_promote_members", "can_manage_topics")


async def _delete_later(msg, delay=5):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


@admin_only
async def purge(update: Update, ctx):
    msg, chat = update.message, update.effective_chat
    if not msg.reply_to_message:
        return await say(update, card(
            "↩️", "reply required", sc("reply to the first message you want to delete")))
    start = msg.reply_to_message.message_id
    ids = list(range(start, msg.message_id + 1))
    if len(ids) > 2000:
        return await say(update, card("❌", "too many messages", sc("max 2000 at once")))
    status = await say(update, card("🧹", "purging..."))
    deleted = 0
    reasons = {}  # why some messages could not be deleted
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            await ctx.bot.delete_messages(chat.id, chunk)
            deleted += len(chunk)
        except Exception:  # fall back to one by one (e.g. very old messages)
            for mid in chunk:
                try:
                    await ctx.bot.delete_message(chat.id, mid)
                    deleted += 1
                except Exception as err:
                    why = str(err)
                    if "not found" not in why.lower():
                        reasons[why] = reasons.get(why, 0) + 1
        await asyncio.sleep(0.3)
    lines = [L("cleared", deleted), L("by", update.effective_user.mention_html())]
    basic = chat.type == "group"
    if basic:
        lines.append(f"<i>{sc('note: this is a basic group, so messages from other bots may stay. turn it into a supergroup to remove them too')}</i>")
    if reasons:
        lines.append(L("couldn't delete", sum(reasons.values())))
        lines.append(f"<i>{esc(max(reasons, key=reasons.get)[:120])}</i>")
    await status.edit_text(card("🧹", "purge complete", *lines), parse_mode=ParseMode.HTML)
    ctx.application.create_task(_delete_later(status, 20 if (basic or reasons) else 5))


async def promoter_rights(chat, user):
    """Rights the invoking user is allowed to hand out (None = may not promote)."""
    if user.id == OWNER_ID:
        return set(ALL_RIGHTS)
    m = await chat.get_member(user.id)
    if m.status == ChatMemberStatus.OWNER:
        return set(ALL_RIGHTS)
    if m.status == ChatMemberStatus.ADMINISTRATOR and m.can_promote_members:
        return {r for r in ALL_RIGHTS if getattr(m, r, False)}
    return None


NO_PROMOTE = card("🔒", "no permission", sc("you need the add admins right to do this"))


@admin_only
async def promote(update: Update, ctx):
    chat = update.effective_chat
    allowed = await promoter_rights(chat, update.effective_user)
    if allowed is None:
        return await say(update, NO_PROMOTE)
    t = await need_target(update, ctx)
    if not t:
        return
    full = any(a.lower() == "all" for a in ctx.args)
    bot_m = await chat.get_member(ctx.bot.id)
    can_give = allowed & {r for r in ALL_RIGHTS if getattr(bot_m, r, False)}
    wanted = ALL_RIGHTS if full else BASIC_RIGHTS
    rights = {r: (r in wanted and r in can_give) for r in ALL_RIGHTS}
    try:
        await chat.promote_member(t.id, can_manage_chat=True, **rights)
    except Exception:
        return await say(update, card(
            "⚠️", "couldn't promote",
            sc("i need the add admins right, and i can only give rights i have myself")))
    given = sum(rights.values())
    await say(update, card(
        "👑", "user promoted", L("user", t.mention_html()),
        L("rights", sc("all") if full else sc("basic")),
        L("granted", f"{given}/{len(wanted)}"),
        L("by", update.effective_user.mention_html())))


@admin_only
async def demote(update: Update, ctx):
    chat = update.effective_chat
    if await promoter_rights(chat, update.effective_user) is None:
        return await say(update, NO_PROMOTE)
    t = await need_target(update, ctx)
    if not t:
        return
    try:
        await chat.promote_member(
            t.id, can_manage_chat=False, **{r: False for r in ALL_RIGHTS})
    except Exception:
        return await say(update, card(
            "⚠️", "couldn't demote",
            sc("i can only demote admins that i promoted")))
    await say(update, card("⬇️", "user demoted", L("user", t.mention_html()),
                           L("by", update.effective_user.mention_html())))


async def adminlist(update: Update, ctx):
    chat = update.effective_chat
    if chat.type == "private":
        return await say(update, card("👥", "groups only", sc("use this command in a group")))
    admins = await chat.get_administrators()
    admins = sorted(admins, key=lambda m: m.status != ChatMemberStatus.OWNER)
    lines = []
    for m in admins:
        owner = m.status == ChatMemberStatus.OWNER
        icon = "👑" if owner else ("🤖" if m.user.is_bot else "🛡")
        line = f"{icon} {m.user.mention_html()}"
        title = getattr(m, "custom_title", None)
        if title:
            line += f" ➜ <i>{esc(title)}</i>"
        elif owner:
            line += f" ➜ <i>{sc('owner')}</i>"
        lines.append(line)
    await say(update, card("👮", "admin list", *lines, "", L("total", len(admins))))


# ---------------------------------------------------------------- warnings
def warn_count(cid, uid):
    row = db.execute("SELECT n FROM warns WHERE chat_id=? AND user_id=?", (cid, uid)).fetchone()
    return row[0] if row else 0


@admin_only
async def warn(update, ctx):
    t = await need_target(update, ctx)
    if not t:
        return
    if await is_protected(update.effective_chat, ctx, t.id):
        return await say(update, PROTECTED)
    cid = update.effective_chat.id
    db.execute("INSERT OR IGNORE INTO warns VALUES(?,?,0)", (cid, t.id))
    db.execute("UPDATE warns SET n=n+1 WHERE chat_id=? AND user_id=?", (cid, t.id))
    db.commit()
    n = warn_count(cid, t.id)
    limit = get_setting(cid, "warn_limit")
    if n >= limit:
        await update.effective_chat.ban_member(t.id)
        db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?", (cid, t.id))
        db.commit()
        await say(update, card("🚫", "user banned", L("user", t.mention_html()),
                               L("reason", f"{limit}/{limit} {sc('warnings')}")))
    else:
        await say(update, card("⚠️", "user warned", L("user", t.mention_html()),
                               L("warns", f"{n}/{limit}")),
                  kb([btn("✅", "remove warn", f"rw:{t.id}", style=OK)]))


@admin_only
async def resetwarns(update, ctx):
    t = await need_target(update, ctx)
    if not t:
        return
    db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?",
               (update.effective_chat.id, t.id))
    db.commit()
    await say(update, card("♻️", "warnings reset", L("user", t.mention_html())))


async def warns_cmd(update, ctx):
    t = await resolve_target(update, ctx) or update.effective_user
    cid = update.effective_chat.id
    await say(update, card("📋", "warnings", L("user", t.mention_html()),
                           L("warns", f"{warn_count(cid, t.id)}/{get_setting(cid, 'warn_limit')}")))


@admin_only
async def setwarnlimit(update, ctx):
    if ctx.args and ctx.args[0].isdigit() and int(ctx.args[0]) > 0:
        set_setting(update.effective_chat.id, "warn_limit", int(ctx.args[0]))
        await say(update, card("✅", "warn limit updated", L("limit", ctx.args[0])))
    else:
        await usage(update, "/setwarnlimit 3")


# ---------------------------------------------------------------- welcome / rules
def fill_welcome(tpl, user, chat):
    """Replace {Username}, {name}, {first}, {fullname}, {mention}, {id}, {group}."""
    def repl(m):
        key = m.group(1).lower()
        first = esc(user.first_name or "")
        if key == "username":
            if user.username:
                return f"@{esc(user.username)}"
            return f'<a href="tg://user?id={user.id}">{first}</a>'
        if key in ("name", "first"):
            return first
        if key == "fullname":
            return esc(user.full_name or "")
        if key == "mention":
            return user.mention_html()
        if key == "id":
            return str(user.id)
        if key == "group":
            return esc(chat.title or "")
        return m.group(0)
    return re.sub(r"\{(\w+)\}", repl, tpl)


async def send_welcome(chat, user):
    tpl = get_setting(chat.id, "welcome")
    media = get_setting(chat.id, "welcome_media")
    file_id = get_setting(chat.id, "welcome_file")
    if tpl or media:
        text = fill_welcome(tpl or "", user, chat)
    else:
        text = f"🎉 <b>{sc('welcome')}</b>\n\n" + fill_welcome("Welcome, {first}!", user, chat)
    markup = None
    if get_setting(chat.id, "rules"):
        markup = kb([btn("📜", "rules", "rules", style=BLUE)])
    kw = dict(parse_mode=ParseMode.HTML)
    if media:
        senders = {"video": chat.send_video, "photo": chat.send_photo,
                   "animation": chat.send_animation, "document": chat.send_document,
                   "audio": chat.send_audio}
        if len(text) <= 1024:
            return await senders[media](file_id, caption=text or None,
                                        reply_markup=markup, **kw)
        await senders[media](file_id)
    return await chat.send_message(text, reply_markup=markup, **kw)


@admin_only
async def setwelcome(update, ctx):
    msg = update.message
    src = msg.reply_to_message
    media = file_id = None
    if src:  # reply to any message -> that message becomes the welcome
        text = (src.text_html if src.text else src.caption_html) or ""
        if src.animation:
            media, file_id = "animation", src.animation.file_id
        elif src.video:
            media, file_id = "video", src.video.file_id
        elif src.photo:
            media, file_id = "photo", src.photo[-1].file_id
        elif src.document:
            media, file_id = "document", src.document.file_id
        elif src.audio:
            media, file_id = "audio", src.audio.file_id
        if not text and not media:
            return await say(update, card(
                "❌", "nothing to save", sc("reply to a text, photo or video message")))
    else:
        parts = (msg.text_html or "").split(None, 1)
        text = parts[1] if len(parts) > 1 else ""
        if not text:
            return await usage(update, "/setwelcome Welcome {Username}!   (or reply to a message)")
    cid = update.effective_chat.id
    set_setting(cid, "welcome", text)
    set_setting(cid, "welcome_media", media)
    set_setting(cid, "welcome_file", file_id)
    await say(update, card(
        "✅", "welcome message saved",
        L("type", sc(media) if media else sc("text")),
        f"▫️ {sc('placeholders')}: <code>{{Username}}</code> <code>{{name}}</code> "
        f"<code>{{group}}</code>",
        f"💡 {sc('send')} /welcome {sc('to preview it')}"))


@admin_only
async def welcome_toggle(update, ctx):
    chat = update.effective_chat
    if ctx.args and ctx.args[0].lower() in ("on", "off"):
        set_setting(chat.id, "welcome_on", int(ctx.args[0].lower() == "on"))
        await say(update, card("👋", f"welcome {ctx.args[0].lower()}"))
    elif not ctx.args:
        await send_welcome(chat, update.effective_user)  # preview
    else:
        await usage(update, "/welcome on|off")


@admin_only
async def resetwelcome(update, ctx):
    cid = update.effective_chat.id
    for col in ("welcome", "welcome_media", "welcome_file"):
        set_setting(cid, col, None)
    await say(update, card("♻️", "welcome reset", sc("back to the default welcome")))


async def on_new_members(update: Update, ctx):
    chat = update.effective_chat
    if not get_setting(chat.id, "welcome_on"):
        return
    for u in update.message.new_chat_members:
        if u.is_bot or db.execute("SELECT 1 FROM fbans WHERE user_id=?", (u.id,)).fetchone():
            continue
        await send_welcome(chat, u)


@admin_only
async def setrules(update, ctx):
    text = " ".join(ctx.args)
    if not text:
        return await usage(update, "/setrules <text>")
    set_setting(update.effective_chat.id, "rules", text)
    await say(update, card("✅", "rules saved"))


def rules_text(chat_id):
    r = get_setting(chat_id, "rules")
    return card("📜", "group rules", esc(r) if r else sc("no rules set yet"))


async def rules(update, ctx):
    await say(update, rules_text(update.effective_chat.id))


# ---------------------------------------------------------------- notes
@admin_only
async def save(update, ctx):
    if not ctx.args:
        return await usage(update, "/save name text  (or reply to a message)")
    name = ctx.args[0].lower()
    text = " ".join(ctx.args[1:])
    if not text and update.message.reply_to_message:
        text = update.message.reply_to_message.text or ""
    if not text:
        return await say(update, card("❌", "nothing to save"))
    db.execute("INSERT OR REPLACE INTO notes VALUES(?,?,?)",
               (update.effective_chat.id, name, text))
    db.commit()
    await say(update, card("✅", "note saved", f"▫️ <code>#{esc(name)}</code>"))


async def get_note(update, ctx):
    if not ctx.args:
        return await usage(update, "/get name")
    row = db.execute("SELECT text FROM notes WHERE chat_id=? AND name=?",
                     (update.effective_chat.id, ctx.args[0].lower())).fetchone()
    await say(update, esc(row[0]) if row else card("❌", "note not found"))


async def notes(update, ctx):
    rows = db.execute("SELECT name FROM notes WHERE chat_id=?", (update.effective_chat.id,)).fetchall()
    if rows:
        await say(update, card("📝", "notes", *[f"▫️ <code>#{esc(r[0])}</code>" for r in rows]))
    else:
        await say(update, card("📝", "notes", sc("no notes saved yet")))


@admin_only
async def clear(update, ctx):
    if not ctx.args:
        return await usage(update, "/clear name")
    db.execute("DELETE FROM notes WHERE chat_id=? AND name=?",
               (update.effective_chat.id, ctx.args[0].lower()))
    db.commit()
    await say(update, card("🗑", "note removed"))


# ---------------------------------------------------------------- filters
@admin_only
async def add_filter(update, ctx):
    if len(ctx.args) < 2:
        return await usage(update, "/filter keyword reply text")
    db.execute("INSERT OR REPLACE INTO filters VALUES(?,?,?)",
               (update.effective_chat.id, ctx.args[0].lower(), " ".join(ctx.args[1:])))
    db.commit()
    await say(update, card("✅", "filter added", f"▫️ <code>{esc(ctx.args[0].lower())}</code>"))


@admin_only
async def stop_filter(update, ctx):
    if not ctx.args:
        return await usage(update, "/stop keyword")
    db.execute("DELETE FROM filters WHERE chat_id=? AND kw=?",
               (update.effective_chat.id, ctx.args[0].lower()))
    db.commit()
    await say(update, card("🗑", "filter removed"))


async def list_filters(update, ctx):
    rows = db.execute("SELECT kw FROM filters WHERE chat_id=?", (update.effective_chat.id,)).fetchall()
    if rows:
        await say(update, card("🔎", "filters", *[f"▫️ <code>{esc(r[0])}</code>" for r in rows]))
    else:
        await say(update, card("🔎", "filters", sc("no filters yet")))


# ---------------------------------------------------------------- antiflood
@admin_only
async def setflood(update, ctx):
    if ctx.args and ctx.args[0].isdigit():
        n = int(ctx.args[0])
        set_setting(update.effective_chat.id, "flood", n)
        await say(update, card("🌊", "antiflood", L("limit", f"{n} {sc('msgs / 5s')}" if n else sc("off"))))
    else:
        await usage(update, "/setflood 5   (0 = off)")


recent = defaultdict(lambda: deque(maxlen=50))


async def on_text(update: Update, ctx):
    msg, chat, user = update.message, update.effective_chat, update.effective_user
    if not msg or not msg.text or chat.type == "private":
        return
    cid = chat.id

    limit = get_setting(cid, "flood")
    if limit:
        q = recent[(cid, user.id)]
        now = time.time()
        q.append(now)
        if sum(1 for t in q if now - t <= 5) >= limit and not await is_admin(chat, user.id):
            q.clear()
            until = datetime.now(timezone.utc) + timedelta(minutes=10)
            await chat.restrict_member(user.id, MUTED, until_date=until)
            return await say(update, card(
                "🌊", "flood detected", L("user", user.mention_html()),
                L("action", sc("muted for 10 minutes"))))

    text = msg.text.lower()

    if text.startswith("#"):
        row = db.execute("SELECT text FROM notes WHERE chat_id=? AND name=?",
                         (cid, text[1:].split()[0])).fetchone()
        if row:
            return await say(update, esc(row[0]))

    for kw, reply in db.execute("SELECT kw, reply FROM filters WHERE chat_id=?", (cid,)):
        if kw in text:
            return await say(update, esc(reply))


# ---------------------------------------------------------------- tagall
tagging, stop_tagging = set(), set()  # chats currently being tagged / asked to stop


async def _run_tagall(chat, users, header):
    tagging.add(chat.id)
    stop_tagging.discard(chat.id)
    sent = 0
    for i in range(0, len(users), 5):
        if chat.id in stop_tagging:
            break
        batch = users[i:i + 5]
        text = f"📢 <b>{header}</b>\n\n" + "\n".join(
            f'🔹 <a href="tg://user?id={uid}">{esc(name or "member")}</a>'
            for uid, name in batch)
        for attempt in range(2):
            try:
                await chat.send_message(text, parse_mode=ParseMode.HTML)
                sent += len(batch)
                break
            except Exception as e:  # flood wait -> wait and retry once
                await asyncio.sleep(getattr(e, "retry_after", 3) + 1)
        await asyncio.sleep(1.5)
    stopped = chat.id in stop_tagging
    tagging.discard(chat.id)
    stop_tagging.discard(chat.id)
    await chat.send_message(
        card("🛑" if stopped else "✅", "tagging stopped" if stopped else "tagging finished",
             L("tagged", sent)), parse_mode=ParseMode.HTML)


@admin_only
async def tagall(update: Update, ctx):
    chat, msg = update.effective_chat, update.message
    if chat.id in tagging:
        return await say(update, card("⏳", "already tagging", f"{sc('use')} /stoptag {sc('to stop')}"))
    custom = " ".join(ctx.args).strip()
    if not custom and msg.reply_to_message:
        custom = msg.reply_to_message.text or msg.reply_to_message.caption or ""
    header = esc(custom[:200]) if custom else sc("attention everyone")

    users = {uid: name for uid, name in db.execute(
        "SELECT user_id, name FROM members WHERE chat_id=?", (chat.id,))
        if name != "Deleted Account"}
    for m in await chat.get_administrators():
        if not m.user.is_bot and m.user.first_name != "Deleted Account":
            users.setdefault(m.user.id, m.user.first_name)
    if not users:
        return await say(update, card("🔎", "no members known yet",
                                      sc("i tag people i have seen in this group")))
    total = await chat.get_member_count()
    await say(update, card(
        "📢", "tagging members", L("tagging", len(users)), L("group size", total),
        f"<i>{sc('only members the bot has seen can be tagged')}</i>",
        f"💡 {sc('stop anytime with')} /stoptag"))
    ctx.application.create_task(_run_tagall(chat, list(users.items()), header))


@admin_only
async def stoptag(update: Update, ctx):
    chat = update.effective_chat
    if chat.id in tagging:
        stop_tagging.add(chat.id)
        await say(update, card("🛑", "stopping...", sc("tagging will stop after this message")))
    else:
        await say(update, card("ℹ️", "nothing to stop", sc("no tagging is running")))


# ---------------------------------------------------------------- owner tools
OWNER_ONLY = card("🔒", "owner only", sc("only the bot owner can use this command"))


def _dead(err):
    t = str(err).lower()
    return any(x in t for x in ("blocked", "kicked", "chat not found", "deactivated",
                                "not a member", "forbidden"))


async def _run_broadcast(bot, report_chat, src, text, targets):
    sent = failed = 0
    for cid in targets:
        for attempt in range(2):
            try:
                if src:
                    await bot.copy_message(cid, src.chat_id, src.message_id)
                else:
                    await bot.send_message(cid, text, parse_mode=ParseMode.HTML)
                sent += 1
                break
            except Exception as e:
                wait = getattr(e, "retry_after", None)
                if wait and attempt == 0:
                    await asyncio.sleep(wait + 1)
                    continue
                failed += 1
                if _dead(e):  # forget chats that no longer exist / blocked the bot
                    db.execute("DELETE FROM chats WHERE chat_id=?", (cid,))
                    db.execute("DELETE FROM bot_users WHERE user_id=?", (cid,))
                    db.commit()
                break
        await asyncio.sleep(0.1)
    await bot.send_message(report_chat, card("📣", "broadcast finished",
                                             L("sent", sent), L("failed", failed)),
                           parse_mode=ParseMode.HTML)


async def broadcast(update: Update, ctx):
    msg = update.message
    if update.effective_user.id != OWNER_ID:
        return await say(update, OWNER_ONLY)
    src = msg.reply_to_message
    parts = (msg.text_html or "").split(None, 1)
    text = parts[1] if len(parts) > 1 else ""
    mode = "all"
    first = text.split(None, 1)
    if first and first[0].lower() in ("-groups", "-g"):
        mode, text = "groups", (first[1] if len(first) > 1 else "")
    elif first and first[0].lower() in ("-users", "-u"):
        mode, text = "users", (first[1] if len(first) > 1 else "")
    if not src and not text:
        return await say(update, card(
            "ℹ️", "usage", "<code>/broadcast your message</code>",
            f"{sc('or reply to a message with')} <code>/broadcast</code>",
            f"{sc('add')} <code>-groups</code> {sc('or')} <code>-users</code> {sc('to limit it')}"))
    targets = []
    if mode in ("all", "groups"):
        targets += [r[0] for r in db.execute(
            "SELECT chat_id FROM chats WHERE type IN ('group','supergroup')")]
    if mode in ("all", "users"):
        targets += [r[0] for r in db.execute("SELECT user_id FROM bot_users")]
    if not targets:
        return await say(update, card("📣", "no chats known yet",
                                      sc("i learn groups and users as they use me")))
    await say(update, card("📣", "broadcast started", L("chats", len(targets))))
    ctx.application.create_task(
        _run_broadcast(ctx.bot, update.effective_chat.id, src, text, targets))


async def resolve_global(update: Update, ctx):
    """Reply, text-mention, numeric id or @username seen in any group."""
    msg = update.message
    if msg.reply_to_message:
        return msg.reply_to_message.from_user
    for ent in msg.entities or []:
        if ent.type == "text_mention" and ent.user:
            return ent.user
    for arg in ctx.args or []:
        if arg.startswith("@") and len(arg) > 1:
            row = db.execute(
                "SELECT user_id, username, name FROM members WHERE lower(username)=? LIMIT 1",
                (arg[1:].lower(),)).fetchone()
            if row:
                return User(id=row[0], first_name=row[2] or row[1] or str(row[0]),
                            is_bot=False, username=row[1])
            return None
        if arg.isdigit() and len(arg) >= 6:
            return User(id=int(arg), first_name=arg, is_bot=False)
    return None


def _groups():
    return [r[0] for r in db.execute(
        "SELECT chat_id FROM chats WHERE type IN ('group','supergroup')")]


async def _run_fban(bot, report_chat, user, groups, lift=False):
    ok = failed = 0
    for gid in groups:
        try:
            if lift:
                await bot.unban_chat_member(gid, user.id, only_if_banned=True)
            else:
                await bot.ban_chat_member(gid, user.id)
            ok += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.1)
    await bot.send_message(report_chat, card(
        "🌐", "federation unban finished" if lift else "federation ban finished",
        L("user", user.mention_html()), L("groups done", ok), L("failed", failed),
        f"<i>{sc('failed = groups where i am not an admin with ban rights')}</i>"),
        parse_mode=ParseMode.HTML)


async def fban(update: Update, ctx):
    if update.effective_user.id != OWNER_ID:
        return await say(update, OWNER_ONLY)
    t = await resolve_global(update, ctx)
    if not t:
        return await say(update, card(
            "🔎", "user not found", sc("reply to the user, or use their @username / id"),
            f"<i>{sc('usernames work for people the bot has seen in a group')}</i>"))
    if t.id in (OWNER_ID, ctx.bot.id):
        return await say(update, PROTECTED)
    args = [a for a in ctx.args if not (a.startswith("@") or a.isdigit())]
    reason = " ".join(args)[:200]
    db.execute("INSERT OR REPLACE INTO fbans VALUES(?,?,?)", (t.id, t.first_name, reason))
    db.commit()
    groups = _groups()
    await say(update, card("🌐", "federation ban started", L("user", t.mention_html()),
                           L("groups", len(groups)),
                           *([L("reason", esc(reason))] if reason else [])))
    ctx.application.create_task(_run_fban(ctx.bot, update.effective_chat.id, t, groups))


async def unfban(update: Update, ctx):
    if update.effective_user.id != OWNER_ID:
        return await say(update, OWNER_ONLY)
    t = await resolve_global(update, ctx)
    if not t:
        return await say(update, card("🔎", "user not found",
                                      sc("reply to the user, or use their @username / id")))
    db.execute("DELETE FROM fbans WHERE user_id=?", (t.id,))
    db.commit()
    fban_done.difference_update({k for k in fban_done if k[1] == t.id})
    groups = _groups()
    await say(update, card("🌐", "federation unban started", L("user", t.mention_html()),
                           L("groups", len(groups))))
    ctx.application.create_task(
        _run_fban(ctx.bot, update.effective_chat.id, t, groups, lift=True))


async def kickme(update: Update, ctx):
    chat, user = update.effective_chat, update.effective_user
    if chat.type == "private":
        return await say(update, card("👥", "groups only", sc("use this command in a group")))
    if await is_admin(chat, user.id):
        return await say(update, card("🔒", "not for admins", sc("only non-admins can use /kickme")))
    await chat.ban_member(user.id)
    await chat.unban_member(user.id)
    await say(update, card("👋", "goodbye", L("user", user.mention_html()),
                           sc("left the group")))


# ---------------------------------------------------------------- member tags
TAG_LINE = re.compile(r"^\s*@?([A-Za-z0-9_]{3,32})\s*[-\u2013\u2014:=]+\s*(.+?)\s*$")


def bad_tag(tag):
    if len(tag) > 16:
        return "max 16 characters"
    if any(ord(c) >= 0x1F000 or unicodedata.category(c) in ("So", "Sk") for c in tag):
        return "emoji not allowed"
    return None


async def apply_tag(bot, chat_id, user_id, tag):
    try:
        await bot.set_chat_member_tag(chat_id, user_id, tag)
        return True, None
    except Exception as e:
        logging.warning("set tag failed in %s for %s: %s", chat_id, user_id, e)
        return False, str(e)


async def addtag(update: Update, ctx):
    msg, user = update.message, update.effective_user
    lines = (msg.text or "").splitlines()
    head = lines[0].split() if lines else []
    body = [ln for ln in lines[1:] if ln.strip()]
    chat_id = None
    if len(head) > 1 and re.fullmatch(r"-?\d+", head[1]):
        chat_id = int(head[1])
    elif update.effective_chat.type != "private":
        chat_id = update.effective_chat.id
    if chat_id is None or not body:
        return await say(update, card(
            "ℹ️", "usage", "<code>/addtag groupid</code>", "<code>@username - Tag</code>",
            "<code>@username - Tag</code>",
            f"<i>{sc('one player per line, tag max 16 characters, no emoji')}</i>"))
    try:
        chat = await ctx.bot.get_chat(chat_id)
    except Exception:
        return await say(update, card("❌", "group not found",
                                      sc("add me to that group first, then check the group id")))
    if not await is_admin(chat, user.id):
        return await say(update, card("🔒", "admins only",
                                      sc("only admins of that group can add tags")))
    results, shown = [], 0
    for ln in body:
        m = TAG_LINE.match(ln)
        if not m:
            results.append(f"❌ <code>{esc(ln[:40])}</code> ➜ {sc('wrong format')}")
            continue
        uname, tag = m.group(1), m.group(2)
        err = bad_tag(tag)
        if err:
            results.append(f"❌ @{esc(uname)} ➜ {sc(err)}")
            continue
        db.execute(
            "INSERT INTO tags(chat_id, username, tag, user_id, applied) VALUES(?,?,?,NULL,0) "
            "ON CONFLICT(chat_id, username) DO UPDATE SET tag=excluded.tag, applied=0",
            (chat_id, uname.lower(), tag))
        row = db.execute("SELECT user_id FROM members WHERE chat_id=? AND lower(username)=?",
                         (chat_id, uname.lower())).fetchone()
        if row:
            ok, why = await apply_tag(ctx.bot, chat_id, row[0], tag)
            if ok:
                db.execute("UPDATE tags SET applied=1, user_id=? WHERE chat_id=? AND username=?",
                           (row[0], chat_id, uname.lower()))
                results.append(f"✅ @{esc(uname)} ➜ <b>{esc(tag)}</b>")
            else:
                results.append(f"⚠️ @{esc(uname)} ➜ {esc((why or '')[:60])}")
        else:
            results.append(f"⏳ @{esc(uname)} ➜ <b>{esc(tag)}</b> "
                           f"<i>({sc('saved, applied when seen')})</i>")
    db.commit()
    extra = f"\n… +{len(results) - 40}" if len(results) > 40 else ""
    await say(update, card("🏷", "tags saved", L("group", esc(chat.title or str(chat_id))),
                           "", *results[:40]) + extra +
              f"\n\n<i>{sc('the bot needs the edit member tags admin right')}</i>")


# ---------------------------------------------------------------- locks
LOCK_TYPES = ("message", "stickers", "gif", "photo", "video", "audio", "voice",
              "document", "videonote", "poll", "link", "forward", "contact", "location")
MEDIA = ("photo", "video", "gif", "audio", "voice", "document", "videonote")
LOCK_ALIASES = {
    "messages": "message", "text": "message", "msg": "message",
    "sticker": "stickers", "gifs": "gif", "animation": "gif",
    "photos": "photo", "pics": "photo", "videos": "video", "voices": "voice",
    "documents": "document", "docs": "document", "files": "document", "file": "document",
    "links": "link", "url": "link", "urls": "link", "forwards": "forward",
    "polls": "poll", "video_note": "videonote", "contacts": "contact",
}
TYPES_HELP = ("<code>all media message stickers gif photo video audio voice "
              "document videonote poll link forward contact location</code>")


def parse_lock_args(args):
    kinds, bad = set(), []
    for a in args:
        a = a.lower().strip(",")
        if a == "all":
            kinds.update(LOCK_TYPES)
        elif a == "media":
            kinds.update(MEDIA)
        else:
            a = LOCK_ALIASES.get(a, a)
            if a in LOCK_TYPES:
                kinds.add(a)
            else:
                bad.append(a)
    return kinds, bad


def locked_kinds(chat_id):
    return {r[0] for r in db.execute("SELECT kind FROM locks WHERE chat_id=?", (chat_id,))}


def _names(kinds):
    if set(LOCK_TYPES) <= set(kinds):
        return sc("everything")
    return " ".join(f"<code>{k}</code>" for k in sorted(kinds))


@admin_only
async def lock(update: Update, ctx):
    chat = update.effective_chat
    kinds, bad = parse_lock_args(ctx.args)
    if not kinds:
        extra = [f"{sc('unknown type')}: <code>{esc(', '.join(bad))}</code>"] if bad else []
        return await say(update, card("ℹ️", "usage", "<code>/lock all</code>",
                                      "<code>/lock message</code>  <code>/lock stickers</code>  <code>/lock gif</code>",
                                      f"{sc('types')}: {TYPES_HELP}", *extra))
    db.executemany("INSERT OR IGNORE INTO locks VALUES(?,?)", [(chat.id, k) for k in kinds])
    db.commit()
    lines = [L("locked", _names(kinds)), f"<i>{sc('admins are not affected')}</i>"]
    try:
        bm = await chat.get_member(ctx.bot.id)
        if not getattr(bm, "can_delete_messages", False):
            lines.append(f"⚠️ {sc('i need the delete messages admin right to enforce this')}")
    except Exception:
        pass
    await say(update, card("🔒", "locked", *lines))


@admin_only
async def unlock(update: Update, ctx):
    chat = update.effective_chat
    kinds, bad = parse_lock_args(ctx.args)
    if not kinds:
        return await say(update, card("ℹ️", "usage", "<code>/unlock all</code>",
                                      "<code>/unlock gif</code>", f"{sc('types')}: {TYPES_HELP}"))
    db.executemany("DELETE FROM locks WHERE chat_id=? AND kind=?", [(chat.id, k) for k in kinds])
    db.commit()
    await say(update, card("🔓", "unlocked", L("unlocked", _names(kinds))))


async def locks_cmd(update: Update, ctx):
    cur = locked_kinds(update.effective_chat.id)
    lines = [f"{'🔒' if k in cur else '🔓'} <code>{k}</code>" for k in LOCK_TYPES]
    await say(update, card("🔐", "locks", *lines, "",
                           f"<i>{sc('admins are never affected')}</i>"))


_admin_cache = {}  # chat_id -> (time, set of admin ids)


async def _admin_ids(chat):
    hit = _admin_cache.get(chat.id)
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    ids = {m.user.id for m in await chat.get_administrators()}
    _admin_cache[chat.id] = (time.time(), ids)
    return ids


def message_kinds(msg):
    k = set()
    if msg.sticker:
        k.add("stickers")
    if msg.animation:
        k.add("gif")
    elif msg.document:
        k.add("document")
    if msg.photo:
        k.add("photo")
    if msg.video:
        k.add("video")
    if msg.audio:
        k.add("audio")
    if msg.voice:
        k.add("voice")
    if msg.video_note:
        k.add("videonote")
    if msg.poll:
        k.add("poll")
    if msg.contact:
        k.add("contact")
    if msg.location or msg.venue:
        k.add("location")
    if msg.forward_origin:
        k.add("forward")
    if any(e.type in ("url", "text_link") for e in (msg.entities or msg.caption_entities or [])):
        k.add("link")
    if msg.text and not msg.text.startswith("/"):  # commands stay usable
        k.add("message")
    return k


async def enforce_locks(update: Update, ctx):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not user or msg.sender_chat or msg.is_automatic_forward:
        return
    if user.id in (OWNER_ID, 1087968824, 136817688):  # owner, anonymous admin, channel bot
        return
    locked = locked_kinds(chat.id)
    if not locked or not (message_kinds(msg) & locked):
        return
    if user.id in await _admin_ids(chat) or await is_admin(chat, user.id):
        return
    try:
        await msg.delete()
    except Exception:
        return  # no delete right
    raise ApplicationHandlerStop


# ---------------------------------------------------------------- id
async def id_cmd(update: Update, ctx):
    chat, user = update.effective_chat, update.effective_user
    msg = update.message
    t = None
    if msg.reply_to_message:
        t = msg.reply_to_message.from_user
    elif chat.type != "private" and ctx.args:
        t = await need_target(update, ctx)  # @username / id lookup
        if not t:
            return
    if t:
        return await say(update, card("🆔", "user id", L("user", t.mention_html()),
                                      L("id", f"<code>{t.id}</code>")))
    if chat.type == "private":
        return await say(update, card("🆔", "your id", L("user", user.mention_html()),
                                      L("id", f"<code>{user.id}</code>")))
    await say(update, card("🆔", "group id", L("group", esc(chat.title or "")),
                           L("id", f"<code>{chat.id}</code>")))


# ---------------------------------------------------------------- translate
async def translate(update: Update, ctx):
    msg, args = update.message, ctx.args
    if msg.reply_to_message:
        src = msg.reply_to_message
        text = src.text or src.caption
        lang = args[0].lower() if args else "en"
    else:
        if len(args) < 2:
            return await say(update, card(
                "ℹ️", "usage",
                "<code>/tr es Hello everyone</code>",
                f"{sc('or reply to a message with')} <code>/tr [lang]</code>"))
        lang, text = args[0].lower(), " ".join(args[1:])
    if not text:
        return await say(update, card("❌", "nothing to translate"))
    try:
        out = await asyncio.to_thread(
            GoogleTranslator(source="auto", target=lang).translate, text)
    except Exception:
        return await say(update, card(
            "❌", "translation failed",
            f"{sc('check the language code')}: <code>en es hi ar fr ...</code>"))
    await say(update, card("🌐", "translation", L("to", f"<code>{esc(lang)}</code>"),
                           "", esc((out or "")[:3500])))


# ---------------------------------------------------------------- deleted accounts
fban_done = set()   # (chat, user) pairs already handled this run
tag_tried = set()   # (chat, user) pairs we already tried to tag this run


async def track(update: Update, ctx):
    """Remember chats and who has been seen (Bot API can't list members);
    also enforces federation bans and gives saved tags."""
    chat, msg = update.effective_chat, update.message
    db.execute(
        "INSERT INTO chats(chat_id, title, type) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, type=excluded.type",
        (chat.id, chat.title, chat.type))
    users = [update.effective_user] if update.effective_user else []
    if msg and msg.new_chat_members:
        users += msg.new_chat_members
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        users.append(msg.reply_to_message.from_user)
    for u in users:
        if u.is_bot:
            continue
        if db.execute("SELECT 1 FROM fbans WHERE user_id=?", (u.id,)).fetchone():
            if (chat.id, u.id) not in fban_done:
                fban_done.add((chat.id, u.id))
                try:
                    await chat.ban_member(u.id)
                    await chat.send_message(card(
                        "🌐", "federation ban", L("user", u.mention_html()),
                        sc("banned from all groups")), parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            continue
        db.execute(
            "INSERT INTO members(chat_id, user_id, username, name) VALUES(?,?,?,?) "
            "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
            "username=excluded.username, name=excluded.name",
            (chat.id, u.id, u.username, u.first_name))
        if u.username and (chat.id, u.id) not in tag_tried:
            row = db.execute(
                "SELECT tag FROM tags WHERE chat_id=? AND username=? AND applied=0",
                (chat.id, u.username.lower())).fetchone()
            if row:
                tag_tried.add((chat.id, u.id))
                ok, _err = await apply_tag(ctx.bot, chat.id, u.id, row[0])
                if ok:
                    db.execute("UPDATE tags SET applied=1, user_id=? "
                               "WHERE chat_id=? AND username=?",
                               (u.id, chat.id, u.username.lower()))
    db.commit()


async def track_private(update: Update, ctx):
    u = update.effective_user
    if u and not u.is_bot:
        db.execute(
            "INSERT INTO bot_users(user_id, name) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name", (u.id, u.first_name))
        db.commit()


async def on_my_chat_member(update: Update, ctx):
    """The bot was added to / removed from a chat (or a user blocked the bot)."""
    ev = update.my_chat_member
    chat, status = ev.chat, ev.new_chat_member.status
    if chat.type == "private":
        if status in (ChatMemberStatus.BANNED, ChatMemberStatus.LEFT):
            db.execute("DELETE FROM bot_users WHERE user_id=?", (chat.id,))
    elif status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        db.execute(
            "INSERT INTO chats(chat_id, title, type) VALUES(?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title, type=excluded.type",
            (chat.id, chat.title, chat.type))
    else:
        db.execute("DELETE FROM chats WHERE chat_id=?", (chat.id,))
    db.commit()


async def scan_zombies(chat, clean):
    ids = [r[0] for r in db.execute("SELECT user_id FROM members WHERE chat_id=?", (chat.id,))]
    found = removed = 0
    for uid in ids:
        try:
            m = await chat.get_member(uid)
        except Exception:
            continue
        if m.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            db.execute("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat.id, uid))
            continue
        if m.user.first_name == "Deleted Account":
            found += 1
            if clean:
                try:
                    await chat.ban_member(uid)
                    await chat.unban_member(uid)
                    db.execute("DELETE FROM members WHERE chat_id=? AND user_id=?", (chat.id, uid))
                    removed += 1
                except Exception:
                    pass
        await asyncio.sleep(0.05)
    db.commit()
    return len(ids), found, removed


def zombie_result(total, found, removed, clean):
    lines = [L("scanned", total), L("found", found)]
    if clean:
        lines.append(L("removed", removed))
    lines.append(f"\n<i>{sc('only members the bot has seen are checked')}</i>")
    markup = None
    if found and not clean:
        markup = kb([btn("🧹", "clean now", "zclean", style=RED)])
    return card("🧟", "deleted accounts", *lines), markup


@admin_only
async def zombies(update: Update, ctx):
    chat = update.effective_chat
    clean = bool(ctx.args) and ctx.args[0].lower() == "clean"
    status = await say(update, card("🔍", "scanning members..."))
    total, found, removed = await scan_zombies(chat, clean)
    text, markup = zombie_result(total, found, removed, clean)
    await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ---------------------------------------------------------------- buttons
async def on_callback(update: Update, ctx):
    q = update.callback_query
    data = q.data or ""
    chat = q.message.chat

    if data == "close":
        await q.message.delete()
        return await q.answer()

    if data.startswith("help:"):
        key = data[5:]
        if key == "main":
            text, markup = help_main_text(), help_menu()
        else:
            emoji, title, lines = PAGES[key]
            text, markup = card(emoji, title, *lines), page_menu()
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
        return await q.answer()

    if data == "rules":
        await q.answer()
        return await q.message.reply_text(rules_text(chat.id), parse_mode=ParseMode.HTML)

    # everything below is admin-only
    if not await is_admin(chat, q.from_user.id):
        return await q.answer(sc("admins only"), show_alert=True)

    action, _, arg = data.partition(":")
    by = L("by", q.from_user.mention_html())

    if action == "unban":
        await chat.unban_member(int(arg), only_if_banned=True)
        text = card("✅", "user unbanned", L("user", user_link(arg)), by)
    elif action == "unmute":
        await chat.restrict_member(int(arg), FULL)
        text = card("🔊", "user unmuted", L("user", user_link(arg)), by)
    elif action == "rw":
        db.execute("UPDATE warns SET n=MAX(n-1,0) WHERE chat_id=? AND user_id=?", (chat.id, int(arg)))
        db.commit()
        n = warn_count(chat.id, int(arg))
        text = card("✅", "warning removed", L("user", user_link(arg)),
                    L("warns", f"{n}/{get_setting(chat.id, 'warn_limit')}"), by)
    elif action == "zclean" or data == "zclean":
        await q.edit_message_text(card("🔍", "cleaning..."), parse_mode=ParseMode.HTML)
        total, found, removed = await scan_zombies(chat, True)
        text, _ = zombie_result(total, found, removed, True)
    else:
        return await q.answer()

    await q.edit_message_text(text, parse_mode=ParseMode.HTML)
    await q.answer(sc("done"))


async def on_error(update, ctx):
    logging.error("Error while handling update", exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                card("⚠️", "couldn't do that",
                     sc("make sure i'm admin with the needed permissions")),
                parse_mode=ParseMode.HTML)
        except Exception:
            pass


# ---------------------------------------------------------------- main
def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing bot token: create config.py (see config.example.py)")
    try:
        import music  # optional: needs API_ID, API_HASH, SESSION_STRING
    except Exception:
        logging.exception("music.py could not be loaded")
        music = None
    builder = (Application.builder().token(BOT_TOKEN)
               .connect_timeout(30).read_timeout(30)
               .write_timeout(30).pool_timeout(30)
               .get_updates_connect_timeout(30).get_updates_read_timeout(30))
    if music:
        builder = builder.post_init(music.start).post_shutdown(music.stop)
    app = builder.build()
    if music:  # registered before the generic button handler
        music.register(app, SimpleNamespace(
            card=card, L=L, sc=sc, esc=esc, btn=btn, kb=kb, say=say, is_admin=is_admin,
            OK=OK, BLUE=BLUE, RED=RED))
    cmds = {
        "start": start, "help": help_cmd,
        "ban": ban, "unban": unban, "kick": kick, "mute": mute, "unmute": unmute,
        "warn": warn, "resetwarns": resetwarns, "warns": warns_cmd,
        "setwarnlimit": setwarnlimit,
        "setwelcome": setwelcome, "welcome": welcome_toggle, "resetwelcome": resetwelcome,
        "setrules": setrules, "rules": rules,
        "save": save, "get": get_note, "notes": notes, "clear": clear,
        "filter": add_filter, "stop": stop_filter, "filters": list_filters,
        "broadcast": broadcast, "fban": fban, "unfban": unfban, "kickme": kickme,
        "addtag": addtag,
        "lock": lock, "unlock": unlock, "locks": locks_cmd,
        "setflood": setflood, "id": id_cmd, "tr": translate, "zombies": zombies,
        "tagall": tagall, "stoptag": stoptag, "purge": purge, "promote": promote, "demote": demote, "adminlist": adminlist,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & ~filters.StatusUpdate.ALL,
                                   enforce_locks), group=-3)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE, track_private), group=-1)
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, track), group=-1)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
