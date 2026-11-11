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
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps

from deep_translator import GoogleTranslator
from telegram import (
    ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update, User,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters,
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
        "▫️ " + sc("target: reply to a user, or use @username / id"),
    ]),
    "tools": ("🧰", "tools", [
        cmd("/setflood N", "mute spammers, N msgs in 5s (0 = off)"),
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
    await update.effective_chat.ban_member(t.id)
    await update.effective_chat.unban_member(t.id)
    await say(update, card("👢", "user kicked", L("user", t.mention_html()),
                           L("by", update.effective_user.mention_html())))


@admin_only
async def mute(update, ctx):
    t = await need_target(update, ctx)
    if not t:
        return
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
                except Exception:
                    pass
        await asyncio.sleep(0.3)
    await status.edit_text(
        card("🧹", "purge complete", L("cleared", deleted),
             L("by", update.effective_user.mention_html())),
        parse_mode=ParseMode.HTML)
    ctx.application.create_task(_delete_later(status))


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
        if not u.is_bot:
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
async def track(update: Update, ctx):
    """Remember who has been seen in each group (Bot API can't list members)."""
    chat, msg = update.effective_chat, update.message
    users = [update.effective_user] if update.effective_user else []
    if msg and msg.new_chat_members:
        users += msg.new_chat_members
    if msg and msg.reply_to_message and msg.reply_to_message.from_user:
        users.append(msg.reply_to_message.from_user)
    for u in users:
        if not u.is_bot:
            db.execute(
                "INSERT INTO members(chat_id, user_id, username, name) VALUES(?,?,?,?) "
                "ON CONFLICT(chat_id, user_id) DO UPDATE SET "
                "username=excluded.username, name=excluded.name",
                (chat.id, u.id, u.username, u.first_name))
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
    app = Application.builder().token(BOT_TOKEN).build()
    cmds = {
        "start": start, "help": help_cmd,
        "ban": ban, "unban": unban, "kick": kick, "mute": mute, "unmute": unmute,
        "warn": warn, "resetwarns": resetwarns, "warns": warns_cmd,
        "setwarnlimit": setwarnlimit,
        "setwelcome": setwelcome, "welcome": welcome_toggle, "resetwelcome": resetwelcome,
        "setrules": setrules, "rules": rules,
        "save": save, "get": get_note, "notes": notes, "clear": clear,
        "filter": add_filter, "stop": stop_filter, "filters": list_filters,
        "setflood": setflood, "tr": translate, "zombies": zombies,
        "purge": purge, "promote": promote, "demote": demote, "adminlist": adminlist,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, track), group=-1)
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, on_new_members))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.run_polling()


if __name__ == "__main__":
    main()
