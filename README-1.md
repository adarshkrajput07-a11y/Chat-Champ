# 𝑴𝑨𝑻𝑹𝑰𝑿 𝑪𝑯𝑨𝑻 𝑩𝑶𝑻 💬🍃

A Telegram group management bot in the style of Miss Rose, built with Python and `python-telegram-bot`.

## Features
- Premium look: small-caps font, bold text, emojis, and colour-styled inline buttons (needs `python-telegram-bot>=22.7`)
- Moderation: `/ban` `/unban` `/kick` `/mute` `/unmute`
- Warnings: `/warn` `/warns` `/resetwarns` `/setwarnlimit`, with an auto-ban at the limit
- Welcome messages: `/setwelcome` `/welcome on|off` (use `{first}` and `{group}`)
- Rules: `/setrules` `/rules`
- Notes: `/save` `/get` `/notes` `/clear`, or `#name`
- Keyword filters: `/filter` `/stop` `/filters`
- Owner: the owner id in `config.py` can use all admin commands in any group
- Antiflood: `/setflood N` (messages in 5 seconds, 0 = off)
- Translate: `/tr [lang]` as a reply, or `/tr lang text` (e.g. `/tr es Hello`)
- Deleted accounts: `/zombies` scans, `/zombies clean` removes them (only members the bot has seen)

## Setup
1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Copy `config.example.py` to `config.py` and fill in your bot token and owner id.
   **Never upload `config.py` to GitHub.**
   Then run:
   ```
   python rosebot.py
   ```
4. Add the bot to your group and make it admin with ban, restrict, and delete permissions.

Data is stored in a local SQLite file (`rosebot.db`).

## Deploy on Railway
1. Push these files to a GitHub repo (do **not** include `config.py`).
2. Create a Railway project from the repo. `railpack.json` sets the start command.
3. In the service's **Variables** tab add:
   - `BOT_TOKEN` = your bot token
   - `OWNER_ID` = your Telegram user id
4. Optional, to keep data across redeploys: add a Volume mounted at `/data` and set `DB_PATH=/data/rosebot.db`.
