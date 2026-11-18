"""
music.py - PREMIUM voice chat music + video player for rosebot.py

rosebot.py already imports this file if it exists:
    music.start(app) / music.stop(app) / music.register(app, ns)

How it works: the bot token handles commands, an "assistant" user account
(SESSION_STRING) joins the group voice chat and streams audio/video.

Needs (config.py or env vars): API_ID, API_HASH, SESSION_STRING
pip install pyrogram tgcrypto py-tgcalls yt-dlp Pillow httpx   (+ ffmpeg + a font)

Highlights
  * generated "now playing" cover card (blurred art, accent colour, title, artist)
  * live progress bar that updates on the card itself
  * full control panel: prev / pause / skip / stop, seek +-10s, volume, mute,
    loop (off / track / queue), shuffle, autoplay, audio <-> video switch,
    queue view, open-on-YouTube link
  * /play /vplay /search (pick from 5 results, audio or video)
  * 720p video streaming
"""
import asyncio
import io
import logging
import os
import random
import time
from collections import deque

import httpx
import yt_dlp
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls import filters as fl
from pytgcalls.types import AudioQuality, MediaStream, VideoQuality
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import CallbackQueryHandler, CommandHandler

try:
    from config import API_ID, API_HASH, SESSION_STRING
except ImportError:
    API_ID = int(os.environ.get("API_ID", "0"))
    API_HASH = os.environ.get("API_HASH", "")
    SESSION_STRING = os.environ.get("SESSION_STRING", "")

if not (API_ID and API_HASH and SESSION_STRING):
    raise RuntimeError("music disabled: set API_ID, API_HASH and SESSION_STRING")

log = logging.getLogger("music")
COOKIES = os.environ.get("COOKIES_FILE")  # optional yt-dlp cookies.txt
BRAND = os.environ.get("MUSIC_BRAND", "MATRIX MUSIC")
TICK = 15          # seconds between live progress-bar refreshes
SEEK_STEP = 10     # seconds for the +-10s buttons
VOL_STEP = 10
HTML = ParseMode.HTML
LOOP_NAMES = ["off", "track", "queue"]

_user: Client = None
_call: PyTgCalls = None
_ns = None
_bot = None
_states = {}
OPEN_ACTIONS = {"queue", "pick"}  # buttons anyone may press


def st(chat_id):
    if chat_id not in _states:
        _states[chat_id] = dict(
            queue=deque(), current=None, prev=[], autoplay=False, loop=0,
            history=deque(maxlen=30), paused=False, muted=False, volume=100,
            offset=0.0, started=0.0, msg=None, photo=False, ticker=None,
            search={}, lock=asyncio.Lock())
    return _states[chat_id]


# ---------------------------------------------------------------- helpers
async def run(fn, *a):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *a)


def fmt_dur(s):
    if s is None:
        return "live"
    s = int(s)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def _short(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def position(s):
    t = s["current"]
    if not t:
        return 0.0
    p = s["offset"] + (0 if s["paused"] else time.monotonic() - s["started"])
    d = t.get("duration")
    return max(0.0, min(p, d) if d else p)


def bar(pos, dur, width=15):
    if not dur:
        return "🔴 LIVE"
    i = min(width - 1, int(width * pos / dur))
    return "━" * i + "●" + "─" * (width - 1 - i)


def parse_time(a):
    """'90' / '1:30' / '+30' / '-15'  ->  (sign, seconds)"""
    sign = 0
    if a[0] in "+-":
        sign = 1 if a[0] == "+" else -1
        a = a[1:]
    secs = 0
    for p in a.split(":"):
        secs = secs * 60 + int(p)
    return sign, secs


# ---------------------------------------------------------------- yt-dlp
def _opts(**extra):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "skip_download": True}
    if COOKIES:
        o["cookiefile"] = COOKIES
    o.update(extra)
    return o


def _entry(e):
    vid = e.get("id")
    url = e.get("webpage_url") or e.get("url")
    if url and not str(url).startswith("http") and vid:
        url = f"https://www.youtube.com/watch?v={vid}"
    is_yt = "youtu" in (url or "")
    thumbs = []
    if is_yt and vid:
        thumbs = [f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
                  f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"]
    else:
        th = e.get("thumbnail") or (e.get("thumbnails") or [{}])[-1].get("url")
        if th:
            thumbs = [th]
    return dict(title=e.get("title") or "Unknown", url=url,
                duration=e.get("duration"), thumbs=thumbs,
                artist=e.get("channel") or e.get("uploader") or "Unknown artist",
                views=e.get("view_count"))


def _search(q, n=1):
    target = q if q.startswith("http") else f"ytsearch{n}:{q}"
    with yt_dlp.YoutubeDL(_opts(extract_flat=True)) as y:
        r = y.extract_info(target, download=False)
    return [_entry(e) for e in (r.get("entries") or [r]) if e]


def _direct(url, video):
    fmt = ("best[height<=720][vcodec!=none][acodec!=none]/best[height<=720]/best"
           if video else "bestaudio/best")
    with yt_dlp.YoutubeDL(_opts(format=fmt)) as y:
        return y.extract_info(url, download=False)["url"]


def _track(entry, video, user):
    return dict(entry, video=video, by=user.first_name, by_id=user.id)


# ---------------------------------------------------------------- cover card
_FONT_DIRS = ["/usr/share/fonts/truetype/dejavu/", "/usr/share/fonts/dejavu/",
              "/usr/share/fonts/truetype/noto/", "/Library/Fonts/", "C:/Windows/Fonts/"]


def _font(size, bold=True):
    names = (["DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf", "arialbd.ttf"] if bold
             else ["DejaVuSans.ttf", "NotoSans-Regular.ttf", "arial.ttf"])
    for d in _FONT_DIRS:
        for nme in names:
            try:
                return ImageFont.truetype(d + nme, size)
            except Exception:
                continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def _clean(text):
    # drop emoji / symbols the card font can't draw
    return "".join(c for c in str(text) if ord(c) < 0x10000 and c not in "\u200d\ufe0f")


def _wrap(draw, text, font, max_w, max_lines):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=font) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines or [""]


def _render_card(raw, t):
    W, H = 1280, 720
    art = None
    if raw:
        try:
            art = Image.open(io.BytesIO(raw)).convert("RGB")
        except Exception:
            art = None
    if art is None:  # gradient fallback
        art = Image.new("RGB", (640, 640))
        d = ImageDraw.Draw(art)
        for y in range(640):
            d.line([(0, y), (640, y)], fill=(int(70 + 60 * y / 640), int(40 + 30 * y / 640),
                                             int(140 + 80 * y / 640)))
        d.text((320, 320), "♪", font=_font(300), fill=(255, 255, 255), anchor="mm")

    avg = art.resize((1, 1), Image.LANCZOS).getpixel((0, 0))
    accent = tuple(min(255, int(c * 1.25) + 45) for c in avg)

    bg = ImageOps.fit(art, (W, H), Image.LANCZOS).filter(ImageFilter.GaussianBlur(38))
    bg = Image.blend(bg, Image.new("RGB", (W, H), (8, 8, 14)), 0.55).convert("RGBA")

    panel = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle(
        (40, 40, W - 40, H - 40), 44, fill=(255, 255, 255, 22),
        outline=(255, 255, 255, 60), width=2)
    bg = Image.alpha_composite(bg, panel)

    size, x0, y0 = 520, 90, 100
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (x0, y0 + 18, x0 + size, y0 + size + 18), 40, fill=(0, 0, 0, 180))
    bg = Image.alpha_composite(bg, shadow.filter(ImageFilter.GaussianBlur(26)))
    cover = ImageOps.fit(art, (size, size), Image.LANCZOS).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), 40, fill=255)
    bg.paste(cover, (x0, y0), mask)

    d = ImageDraw.Draw(bg)
    tx = 660
    maxw = W - 70 - tx

    # badge + label
    badge = "VIDEO" if t.get("video") else "AUDIO"
    bf = _font(24)
    bw = int(d.textlength(badge, font=bf)) + 44
    d.rounded_rectangle((tx, 105, tx + bw, 147), 21, fill=accent)
    d.text((tx + bw // 2, 126), badge, font=bf, fill=(12, 12, 18), anchor="mm")
    d.text((tx + bw + 20, 126), "NOW PLAYING", font=_font(24, False),
           fill=(215, 215, 225), anchor="lm")

    # title / artist
    tf = _font(52)
    lines = _wrap(d, _clean(t["title"]), tf, maxw, 3)
    y = 180
    for ln in lines:
        d.text((tx, y), ln, font=tf, fill=(255, 255, 255))
        y += 64
    d.text((tx, y + 6), _short(_clean(t.get("artist") or ""), 38), font=_font(32, False),
           fill=(205, 205, 218))

    # progress bar (static at 0:00, live one is in the caption)
    by = 468
    d.rounded_rectangle((tx, by, tx + maxw, by + 12), 6, fill=(255, 255, 255, 70))
    d.rounded_rectangle((tx, by, tx + 34, by + 12), 6, fill=accent)
    d.ellipse((tx + 22, by - 8, tx + 42, by + 20), fill=(255, 255, 255))
    sf = _font(26, False)
    d.text((tx, by + 40), "0:00", font=sf, fill=(215, 215, 225), anchor="lm")
    d.text((tx + maxw, by + 40), fmt_dur(t.get("duration")).upper(), font=sf,
           fill=(215, 215, 225), anchor="rm")

    # control icons
    cx, cy = tx + maxw // 2, 575
    d.ellipse((cx - 46, cy - 46, cx + 46, cy + 46), fill=(255, 255, 255))
    d.polygon([(cx - 14, cy - 24), (cx - 14, cy + 24), (cx + 26, cy)], fill=(14, 14, 20))
    for sign in (-1, 1):
        px = cx + sign * 135
        d.polygon([(px + sign * 18, cy - 20), (px + sign * 18, cy + 20), (px - sign * 14, cy)],
                  fill=(255, 255, 255))
        d.rectangle((px - sign * 20, cy - 20, px - sign * 15, cy + 20), fill=(255, 255, 255))

    # footer
    d.text((tx, 650), _short("Requested by " + _clean(t.get("by", "")), 34),
           font=_font(24, False), fill=(190, 190, 205), anchor="lm")
    d.text((tx + maxw, 650), BRAND, font=_font(24), fill=accent, anchor="rm")

    buf = io.BytesIO()
    bg.convert("RGB").save(buf, "JPEG", quality=92)
    return buf.getvalue()


async def _fetch_thumb(urls):
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as c:
        for u in urls:
            try:
                r = await c.get(u)
                if r.status_code == 200 and len(r.content) > 2000:
                    return r.content
            except Exception:
                continue
    return None


async def build_card(t):
    try:
        raw = await _fetch_thumb(t.get("thumbs") or [])
        return await run(_render_card, raw, t)
    except Exception:
        log.exception("card render failed")
        return None


# ---------------------------------------------------------------- UI
def caption(t, s):
    n = _ns
    pos, dur = position(s), t.get("duration")
    nxt = s["queue"][0]["title"] if s["queue"] else None
    lines = [
        f"🎶 <b>{n.esc(_short(t['title'], 70))}</b>",
        f"<code>{fmt_dur(pos)} {bar(pos, dur)} {fmt_dur(dur)}</code>",
        "",
        n.L("artist", n.esc(_short(t.get("artist") or "unknown", 40))),
        n.L("mode", "🎬 video · 720p" if t["video"] else "🎧 audio · high quality"),
        n.L("by", n.esc(t["by"])),
        n.L("volume", f"{'🔇 muted' if s['muted'] else str(s['volume']) + '%'}"
                      f" · loop {LOOP_NAMES[s['loop']]} · auto {'on' if s['autoplay'] else 'off'}"),
    ]
    if nxt:
        lines.append(n.L("next", n.esc(_short(nxt, 40))))
    if s["paused"]:
        lines.insert(2, "⏸ <i>paused</i>")
    return n.card("🎧", "now playing", *lines)


def controls(s):
    n = _ns
    t = s["current"]
    loop_e = ["🔁", "🔂", "🔁"][s["loop"]]
    loop_l = ["loop", "track", "queue"][s["loop"]]
    rows = [
        [n.btn("⏮", "", "mus:prev", style=n.BLUE),
         n.btn("▶️" if s["paused"] else "⏸", "", "mus:pp", style=n.OK),
         n.btn("⏭", "", "mus:skip", style=n.BLUE),
         n.btn("⏹", "", "mus:end", style=n.RED)],
        [n.btn("⏪", "", "mus:back"), n.btn("🔉", "", "mus:vdn"),
         n.btn("🔇" if s["muted"] else "🔈", "", "mus:mute"),
         n.btn("🔊", "", "mus:vup"), n.btn("⏩", "", "mus:fwd")],
        [n.btn(loop_e, loop_l, "mus:loop", style=n.OK if s["loop"] else None),
         n.btn("🔀", "shuffle", "mus:shuf"),
         n.btn("♾", "auto", "mus:auto", style=n.OK if s["autoplay"] else None),
         n.btn("🎧" if t and t["video"] else "🎬", "audio" if t and t["video"] else "video",
               "mus:mode")],
        [n.btn("📜", "queue", "mus:queue"),
         n.btn("🔗", "source", url=t["url"]) if t and t.get("url") else n.btn("🔗", "source", "mus:noop"),
         n.btn("❌", "close", "close", style=n.RED)],
    ]
    return n.kb(*rows)


def queue_text(s):
    n = _ns
    cur, q = s["current"], list(s["queue"])
    if not cur and not q:
        return n.card("📭", "queue is empty")
    lines = []
    if cur:
        lines.append(n.L("now", f"{n.esc(_short(cur['title'], 45))} · {fmt_dur(cur['duration'])}"))
    for i, t in enumerate(q[:10], 1):
        lines.append(f"▫️ <b>{i}.</b> {n.esc(_short(t['title'], 45))} <i>({fmt_dur(t['duration'])})</i>")
    if len(q) > 10:
        lines.append(f"<i>+{len(q) - 10} more</i>")
    if q:
        lines.append(n.L("total", f"{len(q)} songs · {fmt_dur(sum(t.get('duration') or 0 for t in q))}"))
    return n.card("🎶", "queue", *lines)


def queue_kb():
    n = _ns
    return n.kb([n.btn("🔀", "shuffle", "mus:shuf"), n.btn("🗑", "clear", "mus:clear", style=n.RED)],
                [n.btn("❌", "close", "close")])


def _stop_ticker(s):
    tk = s.get("ticker")
    if tk and not tk.done():
        tk.cancel()
    s["ticker"] = None


async def refresh(chat_id):
    s = st(chat_id)
    t, mid = s["current"], s.get("msg")
    if not t or not mid:
        return
    try:
        if s["photo"]:
            await _bot.edit_message_caption(chat_id=chat_id, message_id=mid,
                                            caption=caption(t, s), parse_mode=HTML,
                                            reply_markup=controls(s))
        else:
            await _bot.edit_message_text(caption(t, s), chat_id=chat_id, message_id=mid,
                                         parse_mode=HTML, reply_markup=controls(s))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.debug("refresh failed: %s", e)
    except Exception:
        log.debug("refresh failed", exc_info=True)


async def _ticker(chat_id, t):
    try:
        while True:
            await asyncio.sleep(TICK)
            s = st(chat_id)
            if s["current"] is not t:
                return
            if not s["paused"]:
                await refresh(chat_id)
    except asyncio.CancelledError:
        pass


async def announce(chat_id, t):
    """Send a fresh now-playing card (cover image + live bar + controls)."""
    s = st(chat_id)
    old = s.get("msg")
    _stop_ticker(s)
    try:
        if not t.get("card"):
            t["card"] = await build_card(t)
        if not t["card"]:
            raise RuntimeError("no card")
        m = await _bot.send_photo(chat_id, photo=io.BytesIO(t["card"]), caption=caption(t, s),
                                  parse_mode=HTML, reply_markup=controls(s))
        s["photo"] = True
    except Exception:
        log.warning("photo card failed, using text", exc_info=True)
        m = await _bot.send_message(chat_id, caption(t, s), parse_mode=HTML,
                                    reply_markup=controls(s))
        s["photo"] = False
    s["msg"] = m.message_id
    if old:
        try:
            await _bot.delete_message(chat_id, old)
        except Exception:
            pass
    s["ticker"] = asyncio.create_task(_ticker(chat_id, t))


async def retire(chat_id, text):
    """Turn the current card into a plain final message (no buttons)."""
    s = st(chat_id)
    mid, s["msg"] = s.get("msg"), None
    _stop_ticker(s)
    if not mid:
        return
    try:
        if s["photo"]:
            await _bot.edit_message_caption(chat_id=chat_id, message_id=mid, caption=text,
                                            parse_mode=HTML, reply_markup=None)
        else:
            await _bot.edit_message_text(text, chat_id=chat_id, message_id=mid, parse_mode=HTML)
    except Exception:
        pass


# ---------------------------------------------------------------- playback core
async def ensure_assistant(chat_id):
    """Make sure the assistant account is in the group."""
    try:
        await _user.get_chat_member(chat_id, "me")
        return
    except Exception:
        pass
    link = await _bot.export_chat_invite_link(chat_id)
    await _user.join_chat(link)


async def _in_call(chat_id):
    try:
        return chat_id in (await _call.calls)
    except Exception:
        return True  # can't tell -> assume yes


async def start_track(chat_id, t, seek=0):
    s = st(chat_id)
    jobs = [run(_direct, t["url"], t["video"])]
    if "card" not in t:
        jobs.append(build_card(t))
    res = await asyncio.gather(*jobs)
    url = res[0]
    if len(res) > 1:
        t["card"] = res[1]
    kw = dict(
        audio_parameters=AudioQuality.HIGH,
        video_parameters=VideoQuality.HD_720p,
        video_flags=(MediaStream.Flags.AUTO_DETECT if t["video"] else MediaStream.Flags.IGNORE),
    )
    if seek:
        kw["ffmpeg_parameters"] = f"-ss {int(seek)}"
    await _call.play(chat_id, MediaStream(url, **kw))
    s.update(current=t, offset=float(seek), started=time.monotonic(), paused=False, muted=False)
    if not seek:
        s["history"].append(t["url"])
    if s["volume"] != 100:
        try:
            await _call.change_volume_call(chat_id, s["volume"])
        except Exception:
            pass


async def play_next(chat_id, auto=False):
    """Move to the next song (song ended if auto, otherwise skipped).
    Returns the new track or None when everything is finished."""
    s = st(chat_id)
    last = s["current"]

    def pick():
        if auto and s["loop"] == 1 and last:
            return last
        if last and s["loop"] == 2:
            s["queue"].append(last)
        return s["queue"].popleft() if s["queue"] else None

    nxt = pick()
    if not nxt and s["autoplay"] and last:
        try:
            for r in await run(_search, last["title"], 6):
                if r["url"] not in s["history"]:
                    nxt = dict(r, video=last["video"], by="autoplay", by_id=last.get("by_id"))
                    break
        except Exception:
            log.exception("autoplay search failed")
    while nxt:
        try:
            await start_track(chat_id, nxt)
            if last and nxt is not last:
                s["prev"].append(last)
                del s["prev"][:-20]
            return nxt
        except Exception:
            log.exception("could not start next track")
            nxt = s["queue"].popleft() if s["queue"] else None
    s["current"] = None
    try:
        await _call.leave_call(chat_id)
    except Exception:
        pass
    return None


async def add_track(chat, t):
    """Queue a track, or start it if nothing is playing. Returns 0 when it started
    playing, otherwise its queue position."""
    s = st(chat.id)
    async with s["lock"]:
        if s["current"] and not await _in_call(chat.id):  # voice chat was closed
            s["current"] = None
            s["queue"].clear()
        if s["current"]:
            s["queue"].append(t)
            return len(s["queue"])
        await ensure_assistant(chat.id)
        await start_track(chat.id, t)
        s["prev"].clear()
        await announce(chat.id, t)
        return 0


async def do_toggle_pause(cid, want=None):
    """want=True pauses, False resumes, None toggles. Returns paused state."""
    s = st(cid)
    pause = (not s["paused"]) if want is None else want
    if pause == s["paused"]:
        return s["paused"]
    if pause:
        s["offset"] = position(s)
        await _call.pause(cid)
        s["paused"] = True
    else:
        await _call.resume(cid)
        s["started"] = time.monotonic()
        s["paused"] = False
    return s["paused"]


async def do_prev(cid):
    s = st(cid)
    if not s["prev"]:
        return None
    p = s["prev"].pop()
    if s["current"]:
        s["queue"].appendleft(s["current"])
    await start_track(cid, p)
    return p


async def do_seek(cid, pos):
    s = st(cid)
    t = s["current"]
    if not t or not t.get("duration"):
        return None
    pos = max(0, min(int(pos), int(t["duration"]) - 3))
    was_paused = s["paused"]
    await start_track(cid, t, seek=pos)
    if was_paused:
        await do_toggle_pause(cid, True)
    return pos


async def do_volume(cid, vol):
    s = st(cid)
    vol = max(1, min(200, int(vol)))
    await _call.change_volume_call(cid, vol)
    s["volume"] = vol
    return vol


async def do_mute(cid):
    s = st(cid)
    if s["muted"]:
        await _call.unmute(cid)
    else:
        await _call.mute(cid)
    s["muted"] = not s["muted"]
    return s["muted"]


async def do_mode(cid):
    """Switch the current song between audio and video, keeping the position."""
    s = st(cid)
    pos = int(position(s))
    t2 = dict(s["current"], video=not s["current"]["video"])
    t2.pop("card", None)  # badge on the cover changes
    await start_track(cid, t2, seek=pos)
    return t2


async def do_end(cid):
    s = st(cid)
    s["queue"].clear()
    s["prev"].clear()
    s["current"] = None
    try:
        await _call.leave_call(cid)
    except Exception:
        pass
    await retire(cid, _ns.card("⏹", "stopped", _ns.sc("left the voice chat")))


def do_shuffle(cid):
    s = st(cid)
    items = list(s["queue"])
    random.shuffle(items)
    s["queue"] = deque(items)
    return len(items)


def _fail_msg(e):
    name = type(e).__name__
    if "NoActiveGroupCall" in name:
        return "start a voice chat first, then use /play"
    if isinstance(e, yt_dlp.utils.DownloadError):
        return "couldn't fetch that (blocked, private or age-restricted)"
    return "couldn't play that. is the bot admin (invite users)?"


async def on_stream_end(_, update):
    chat_id = update.chat_id
    s = st(chat_id)
    if not s["current"]:  # we stopped it ourselves
        return
    t = await play_next(chat_id, auto=True)
    if t:
        await announce(chat_id, t)
    else:
        await retire(chat_id, _ns.card("✅", "queue finished", _ns.sc("thanks for listening")))


# ---------------------------------------------------------------- commands
async def _play(update, ctx, video):
    n = _ns
    chat, user, msg = update.effective_chat, update.effective_user, update.effective_message
    if chat.type == "private":
        return await n.say(update, n.card("👥", "groups only", n.sc("use this in a group")))
    query = " ".join(ctx.args or [])
    if not query and msg.reply_to_message:
        query = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()
    if not query:
        return await n.say(update, n.card(
            "ℹ️", "usage", f"<code>/{'vplay' if video else 'play'} song name or link</code>"))
    status = await n.say(update, n.card("🔍", "searching...", n.L("query", n.esc(_short(query, 60)))))
    try:
        res = await run(_search, query, 1)
        if not res:
            return await status.edit_text(n.card("🔎", "nothing found"), parse_mode=HTML)
        t = _track(res[0], video, user)
        pos = await add_track(chat, t)
        if pos == 0:
            try:
                await status.delete()
            except Exception:
                pass
        else:
            await status.edit_text(
                n.card("➕", "added to queue", n.L("title", n.esc(_short(t["title"], 60))),
                       n.L("duration", fmt_dur(t["duration"])),
                       n.L("mode", "video" if video else "audio"),
                       n.L("position", pos), n.L("by", n.esc(user.first_name))),
                parse_mode=HTML,
                reply_markup=n.kb([n.btn("📜", "queue", "mus:queue"), n.btn("❌", "close", "close")]))
    except Exception as e:
        log.exception("play failed")
        await status.edit_text(n.card("⚠️", "play failed", n.sc(_fail_msg(e))), parse_mode=HTML)


async def play_cmd(update, ctx):
    await _play(update, ctx, False)


async def vplay_cmd(update, ctx):
    await _play(update, ctx, True)


async def search_cmd(update, ctx):
    n = _ns
    chat = update.effective_chat
    if chat.type == "private":
        return await n.say(update, n.card("👥", "groups only", n.sc("use this in a group")))
    query = " ".join(ctx.args or [])
    if not query:
        return await n.say(update, n.card("ℹ️", "usage", "<code>/search song name</code>"))
    status = await n.say(update, n.card("🔍", "searching...", n.L("query", n.esc(_short(query, 60)))))
    try:
        res = await run(_search, query, 5)
    except Exception as e:
        log.exception("search failed")
        return await status.edit_text(n.card("⚠️", "search failed", n.sc(_fail_msg(e))), parse_mode=HTML)
    if not res:
        return await status.edit_text(n.card("🔎", "nothing found"), parse_mode=HTML)
    lines = [f"▫️ <b>{i}.</b> {n.esc(_short(r['title'], 50))} <i>({fmt_dur(r['duration'])})</i>"
             for i, r in enumerate(res, 1)]
    lines.append("")
    lines.append(n.sc("🎧 audio  ·  🎬 video"))
    s = st(chat.id)
    s["search"][status.message_id] = res
    while len(s["search"]) > 10:
        s["search"].pop(next(iter(s["search"])))
    rows = [[n.btn("🎧", str(i), f"mus:pick:{i - 1}:0", style=n.BLUE) for i in range(1, len(res) + 1)],
            [n.btn("🎬", str(i), f"mus:pick:{i - 1}:1", style=n.OK) for i in range(1, len(res) + 1)],
            [n.btn("❌", "close", "close", style=n.RED)]]
    await status.edit_text(n.card("🔎", "search results", *lines), parse_mode=HTML,
                           reply_markup=n.kb(*rows))


async def _guard(update):
    n = _ns
    chat, user = update.effective_chat, update.effective_user
    if chat.type == "private":
        await n.say(update, n.card("👥", "groups only", n.sc("use this in a group")))
        return False
    t = st(chat.id)["current"]
    if await n.is_admin(chat, user.id) or (t and t.get("by_id") == user.id):
        return True
    await n.say(update, n.card("🔒", "admins only", n.sc("only admins or the song requester")))
    return False


async def _playing(update):
    if not st(update.effective_chat.id)["current"]:
        await _ns.say(update, _ns.card("🔇", "nothing is playing"))
        return False
    return True


async def pause_cmd(update, ctx):
    if await _guard(update) and await _playing(update):
        await do_toggle_pause(update.effective_chat.id, True)
        await refresh(update.effective_chat.id)
        await _ns.say(update, _ns.card("⏸", "paused"))


async def resume_cmd(update, ctx):
    if await _guard(update) and await _playing(update):
        await do_toggle_pause(update.effective_chat.id, False)
        await refresh(update.effective_chat.id)
        await _ns.say(update, _ns.card("▶️", "resumed"))


async def skip_cmd(update, ctx):
    if not (await _guard(update) and await _playing(update)):
        return
    cid = update.effective_chat.id
    t = await play_next(cid)
    if t:
        await announce(cid, t)
    else:
        await retire(cid, _ns.card("⏹", "queue finished"))


async def back_cmd(update, ctx):
    if not (await _guard(update) and await _playing(update)):
        return
    cid = update.effective_chat.id
    p = await do_prev(cid)
    if p:
        await announce(cid, p)
    else:
        await _ns.say(update, _ns.card("⏮", "no previous song"))


async def end_cmd(update, ctx):
    if await _guard(update):
        await do_end(update.effective_chat.id)
        await _ns.say(update, _ns.card("⏹", "stopped", _ns.sc("left the voice chat")))


async def queue_cmd(update, ctx):
    await _ns.say(update, queue_text(st(update.effective_chat.id)), queue_kb())


async def np_cmd(update, ctx):
    cid = update.effective_chat.id
    s = st(cid)
    if not s["current"]:
        return await _ns.say(update, _ns.card("🔇", "nothing is playing"))
    await announce(cid, s["current"])


async def autoplay_cmd(update, ctx):
    if not await _guard(update):
        return
    s = st(update.effective_chat.id)
    s["autoplay"] = not s["autoplay"]
    await refresh(update.effective_chat.id)
    await _ns.say(update, _ns.card("♾", "autoplay", _ns.L("status", "on" if s["autoplay"] else "off")))


async def loop_cmd(update, ctx):
    if not await _guard(update):
        return
    s = st(update.effective_chat.id)
    arg = (ctx.args[0].lower() if ctx.args else "")
    s["loop"] = LOOP_NAMES.index(arg) if arg in LOOP_NAMES else (s["loop"] + 1) % 3
    await refresh(update.effective_chat.id)
    await _ns.say(update, _ns.card("🔁", "loop", _ns.L("mode", LOOP_NAMES[s["loop"]])))


async def shuffle_cmd(update, ctx):
    if await _guard(update):
        n_ = do_shuffle(update.effective_chat.id)
        await _ns.say(update, _ns.card("🔀", "shuffled", _ns.L("songs", n_)))


async def clearqueue_cmd(update, ctx):
    if await _guard(update):
        s = st(update.effective_chat.id)
        c = len(s["queue"])
        s["queue"].clear()
        await _ns.say(update, _ns.card("🗑", "queue cleared", _ns.L("removed", c)))


async def remove_cmd(update, ctx):
    n = _ns
    if not await _guard(update):
        return
    s = st(update.effective_chat.id)
    try:
        i = int(ctx.args[0])
        if i < 1:
            raise IndexError
        t = s["queue"][i - 1]
        del s["queue"][i - 1]
    except (ValueError, IndexError):
        return await n.say(update, n.card("ℹ️", "usage", "<code>/remove position</code>"))
    await n.say(update, n.card("🗑", "removed", n.L("title", n.esc(_short(t["title"], 60)))))


async def seek_cmd(update, ctx):
    n = _ns
    if not (await _guard(update) and await _playing(update)):
        return
    cid = update.effective_chat.id
    s = st(cid)
    try:
        sign, secs = parse_time(ctx.args[0])
    except (ValueError, IndexError):
        return await n.say(update, n.card(
            "ℹ️", "usage", "<code>/seek 1:30</code>  <code>/seek +30</code>  <code>/seek -15</code>"))
    target = secs if sign == 0 else position(s) + sign * secs
    try:
        pos = await do_seek(cid, target)
    except Exception:
        log.exception("seek failed")
        pos = None
    if pos is None:
        return await n.say(update, n.card("⚠️", "can't seek", n.sc("live streams can't be seeked")))
    await refresh(cid)
    await n.say(update, n.card("⏩", "seeked", n.L("position", fmt_dur(pos))))


async def volume_cmd(update, ctx):
    n = _ns
    if not (await _guard(update) and await _playing(update)):
        return
    cid = update.effective_chat.id
    try:
        vol = await do_volume(cid, ctx.args[0])
    except (ValueError, IndexError):
        return await n.say(update, n.card("ℹ️", "usage", "<code>/volume 1-200</code>"))
    except Exception:
        log.exception("volume failed")
        return
    await refresh(cid)
    await n.say(update, n.card("🔊", "volume", n.L("level", f"{vol}%")))


async def mutevc_cmd(update, ctx):
    if await _guard(update) and await _playing(update):
        cid = update.effective_chat.id
        if not st(cid)["muted"]:
            await do_mute(cid)
        await refresh(cid)
        await _ns.say(update, _ns.card("🔇", "muted"))


async def unmutevc_cmd(update, ctx):
    if await _guard(update) and await _playing(update):
        cid = update.effective_chat.id
        if st(cid)["muted"]:
            await do_mute(cid)
        await refresh(cid)
        await _ns.say(update, _ns.card("🔈", "unmuted"))


# ---------------------------------------------------------------- buttons
async def _after(q, cid):
    """Redraw whichever message the button lives on."""
    s = st(cid)
    if q.message.message_id == s.get("msg"):
        await refresh(cid)
    else:
        try:
            await q.edit_message_text(queue_text(s), parse_mode=HTML, reply_markup=queue_kb())
        except BadRequest:
            pass


async def on_button(update, ctx):
    n = _ns
    q = update.callback_query
    chat = q.message.chat
    cid, uid = chat.id, q.from_user.id
    parts = q.data.split(":")
    act = parts[1] if len(parts) > 1 else ""
    s = st(cid)
    t = s["current"]

    if act == "noop":
        return await q.answer()
    if act not in OPEN_ACTIONS:
        if not (await n.is_admin(chat, uid) or (t and t.get("by_id") == uid)):
            return await q.answer(n.sc("only admins or the song requester"), show_alert=True)
        if not t and act not in ("shuf", "clear"):
            return await q.answer(n.sc("nothing is playing"), show_alert=True)

    toast = ""
    try:
        if act == "pp":
            toast = "⏸ paused" if await do_toggle_pause(cid) else "▶️ resumed"
            await _after(q, cid)
        elif act == "skip":
            nt = await play_next(cid)
            if nt:
                await announce(cid, nt)
            else:
                await retire(cid, n.card("⏹", "queue finished"))
            toast = "⏭ skipped"
        elif act == "prev":
            p = await do_prev(cid)
            if p:
                await announce(cid, p)
                toast = "⏮ previous"
            else:
                toast = "no previous song"
        elif act == "end":
            await do_end(cid)
            toast = "⏹ stopped"
        elif act in ("back", "fwd"):
            step = -SEEK_STEP if act == "back" else SEEK_STEP
            pos = await do_seek(cid, position(s) + step)
            if pos is None:
                toast = "live streams can't be seeked"
            else:
                toast = f"{'⏪' if step < 0 else '⏩'} {fmt_dur(pos)}"
                await _after(q, cid)
        elif act in ("vup", "vdn"):
            vol = await do_volume(cid, s["volume"] + (VOL_STEP if act == "vup" else -VOL_STEP))
            toast = f"🔊 {vol}%"
            await _after(q, cid)
        elif act == "mute":
            toast = "🔇 muted" if await do_mute(cid) else "🔈 unmuted"
            await _after(q, cid)
        elif act == "loop":
            s["loop"] = (s["loop"] + 1) % 3
            toast = f"🔁 loop {LOOP_NAMES[s['loop']]}"
            await _after(q, cid)
        elif act == "shuf":
            toast = f"🔀 shuffled {do_shuffle(cid)} songs"
            await _after(q, cid)
        elif act == "clear":
            c = len(s["queue"])
            s["queue"].clear()
            toast = f"🗑 removed {c} songs"
            await _after(q, cid)
        elif act == "auto":
            s["autoplay"] = not s["autoplay"]
            toast = f"♾ autoplay {'on' if s['autoplay'] else 'off'}"
            await _after(q, cid)
        elif act == "mode":
            await q.answer("switching...")
            t2 = await do_mode(cid)
            await announce(cid, t2)
            return
        elif act == "queue":
            await n.say(update, queue_text(s), queue_kb())
        elif act == "pick":
            res = s["search"].get(q.message.message_id)
            if not res:
                return await q.answer(n.sc("this search expired"), show_alert=True)
            nt = _track(res[int(parts[2])], parts[3] == "1", q.from_user)
            await q.answer("loading...")
            pos = await add_track(chat, nt)
            s["search"].pop(q.message.message_id, None)
            if pos == 0:
                await q.message.delete()
            else:
                await q.edit_message_text(
                    n.card("➕", "added to queue", n.L("title", n.esc(_short(nt["title"], 60))),
                           n.L("position", pos), n.L("by", n.esc(q.from_user.first_name))),
                    parse_mode=HTML,
                    reply_markup=n.kb([n.btn("📜", "queue", "mus:queue"), n.btn("❌", "close", "close")]))
            return
    except Exception as e:
        log.exception("button action failed")
        toast = _fail_msg(e)[:180]
    try:
        await q.answer(toast or None)
    except Exception:
        pass


# ---------------------------------------------------------------- hooks for rosebot.py
async def start(app):
    global _user, _call, _bot
    _bot = app.bot
    _user = Client("assistant", api_id=API_ID, api_hash=API_HASH,
                   session_string=SESSION_STRING, in_memory=True)
    _call = PyTgCalls(_user)
    try:
        flt = fl.stream_end()
    except TypeError:
        flt = fl.stream_end
    _call.on_update(flt)(on_stream_end)
    await _user.start()
    await _call.start()
    log.info("music assistant started")


async def stop(app):
    for s in _states.values():
        _stop_ticker(s)
    try:
        await _user.stop()
    except Exception:
        pass


def register(app, ns):
    global _ns
    _ns = ns
    cmds = {
        "play": play_cmd, "vplay": vplay_cmd, "search": search_cmd,
        "pause": pause_cmd, "resume": resume_cmd,
        "skip": skip_cmd, "next": skip_cmd, "back": back_cmd, "prev": back_cmd,
        "end": end_cmd, "stopmusic": end_cmd,
        "queue": queue_cmd, "np": np_cmd, "nowplaying": np_cmd,
        "autoplay": autoplay_cmd, "loop": loop_cmd, "shuffle": shuffle_cmd,
        "clearqueue": clearqueue_cmd, "remove": remove_cmd,
        "seek": seek_cmd, "volume": volume_cmd,
        "mutevc": mutevc_cmd, "unmutevc": unmutevc_cmd,
    }
    for name, fn in cmds.items():
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^mus:"))
