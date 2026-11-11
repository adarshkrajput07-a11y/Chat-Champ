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
import sqlite3
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps

from deep_translator import GoogleTranslator
from telegram import (
    ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Update,
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


def target(update: Update):
    r = update.message.reply_to_message
    return r.from_user if r else None


async def need_target(update: Update):
    t = target(update)
    if not t:
        await say(update, card("↩️", "reply required", sc("reply to a user's message")))
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
    ]),
    "warn": ("⚠️", "warnings", [
        cmd("/warn", "warn a user (reply)"),
        cmd("/warns", "check warnings"),
        cmd("/resetwarns", "clear a user's warnings"),
        cmd("/setwarnlimit N", "warnings before auto-ban"),
    ]),
    "welcome": ("👋", "welcome & rules", [
        "▫️ <code>/setwelcome text</code> ➜ " + sc("set welcome, use ")
        + "<code>{first}</code> <code>{group}</code>",
        cmd("/welcome on|off", "toggle welcome message"),
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
        [btn("✖️", "close", "close", style=RED)],
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
    t = await need_target(update)
    if not t:
        return
    await update.effective_chat.ban_member(t.id)
    await say(update, card("🔨", "user banned", L("user", t.mention_html()),
                           L("by", update.effective_user.mention_html())),
              kb([btn("↩️", "unban", f"unban:{t.id}", style=OK)]))


@admin_only
async def unban(update, ctx):
    t = await need_target(update)
    if not t:
        return
    await update.effective_chat.unban_member(t.id, only_if_banned=True)
    await say(update, card("✅", "user unbanned", L("user", t.mention_html())))


@admin_only
async def kick(update, ctx):
    t = await need_target(update)
    if not t:
        return
    await update.effective_chat.ban_member(t.id)
    await update.effective_chat.unban_member(t.id)
    await say(update, card("👢", "user kicked", L("user", t.mention_html()),
                           L("by", update.effective_user.mention_html())))


@admin_only
async def mute(update, ctx):
    t = await need_target(update)
    if not t:
        return
    await update.effective_chat.restrict_member(t.id, MUTED)
    await say(update, card("🔇", "user muted", L("user", t.mention_html()),
                           L("by", update.effective_user.mention_html())),
              kb([btn("🔊", "unmute", f"unmute:{t.id}", style=OK)]))


@admin_only
async def unmute(update, ctx):
    t = await need_target(update)
    if not t:
        return
    await update.effective_chat.restrict_member(t.id, FULL)
    await say(update, card("🔊", "user unmuted", L("user", t.mention_html())))


# ---------------------------------------------------------------- warnings
def warn_count(cid, uid):
    row = db.execute("SELECT n FROM warns WHERE chat_id=? AND user_id=?", (cid, uid)).fetchone()
    return row[0] if row else 0


@admin_only
async def warn(update, ctx):
    t = await need_target(update)
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
    t = await need_target(update)
    if not t:
        return
    db.execute("DELETE FROM warns WHERE chat_id=? AND user_id=?",
               (update.effective_chat.id, t.id))
    db.commit()
    await say(update, card("♻️", "warnings reset", L("user", t.mention_html())))


async def warns_cmd(update, ctx):
    t = target(update) or update.effective_user
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
@admin_only
async def setwelcome(update, ctx):
    text = " ".join(ctx.args)
    if not text:
        return await usage(update, "/setwelcome Hi {first}, welcome to {group}!")
    set_setting(update.effective_chat.id, "welcome", text)
    await say(update, card("✅", "welcome message saved"))


@admin_only
async def welcome_toggle(update, ctx):
    if ctx.args and ctx.args[0].lower() in ("on", "off"):
        set_setting(update.effective_chat.id, "welcome_on", int(ctx.args[0].lower() == "on"))
        await say(update, card("👋", f"welcome {ctx.args[0].lower()}"))
    else:
        await usage(update, "/welcome on|off")


async def on_new_members(update: Update, ctx):
    chat = update.effective_chat
    if not get_setting(chat.id, "welcome_on"):
        return
    tpl = get_setting(chat.id, "welcome") or "Welcome, {first}!"
    for u in update.message.new_chat_members:
        if u.is_bot:
            continue
        body = (esc(tpl).replace("{first}", f"<b>{esc(u.first_name)}</b>")
                .replace("{group}", f"<b>{esc(chat.title or '')}</b>"))
        await say(update, f"🎉 <b>{sc('welcome')}</b>\n\n{body}",
                  kb([btn("📜", "rules", "rules", style=BLUE)]))


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
    for u in users:
        if not u.is_bot:
            db.execute("INSERT OR IGNORE INTO members VALUES(?,?)", (chat.id, u.id))
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
        "setwelcome": setwelcome, "welcome": welcome_toggle,
        "setrules": setrules, "rules": rules,
        "save": save, "get": get_note, "notes": notes, "clear": clear,
        "filter": add_filter, "stop": stop_filter, "filters": list_filters,
        "setflood": setflood, "tr": translate, "zombies": zombies,
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
