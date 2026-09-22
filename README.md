# 🚀 4ST Prime Core — Telegram Userbot

A powerful Python-based Telegram Userbot & Bot framework with admin tools, AI, plugins, and GitHub-backed persistence.

---

## ✨ Features

- ⚡ Fast Performance
- 🤖 Telegram Userbot + Assistant Bot (Telethon)
- 🎨 Premium Emoji engine
- 🛠 Modular command system — easy plugin additions
- ☁️ GitHub-backed durable persistence (survives Heroku restarts)
- 🔄 24/7 Hosting on Heroku

---

## 📱 Termux profile (music excluded)

Termux runs the complete userbot/admin/AI/plugin feature set **without music,
voice-chat, Pyrogram, PyTgCalls, yt-dlp, or ffmpeg**. This keeps installation
reliable on Android and avoids native voice-chat build failures.

The default installer skips the optional `google-genai` package because its
`cryptography` dependency may compile for a long time on Android/Python 3.13.
The core bot works without it; install the AI add-on separately after setup if
you need Gemini features.

The launcher asks for the Telegram values on every interactive run. Existing
values appear as defaults, so press **Enter** to keep them or enter a new bot
token/session when one has been revoked; the answers are saved in `.env`.

## 🔌 Plugin System

Add new commands without touching `main.py` — just drop a `.py` file in `plugins/`:

```bash
# 1. Copy the template
cp plugins/example_plugin.py plugins/my_feature.py

# 2. Implement your commands in setup()
# 3. Restart bot — loads automatically
```

See [`plugins/README.md`](plugins/README.md) for full docs and the API reference.

---

## 📂 Repo Structure

```
4st_userbot/
├── main.py                  # Core bot logic, all built-in commands
├── github_store.py          # GitHub-backed config persistence
├── plugins/                 # ← Add new features here (auto-loaded)
│   ├── __init__.py          # Plugin loader
│   ├── README.md            # Plugin docs + API reference
│   └── example_plugin.py   # Copy-paste template
├── data/
│   └── config.json          # Runtime config (auto-synced to GitHub)
├── requirements.txt          # cloud profile
├── requirements-termux.txt  # Termux profile without music
├── .env.example             # All supported environment variables
├── Aptfile                  # Cloud system dependencies
└── Procfile                 # Heroku process definition
```

---

## 🚀 Deploy

### Deploy to Termux

```bash
# Install Termux from F-Droid, then run:
pkg update -y
pkg install -y git
git clone https://github.com/oyevipthoma88/4st_userbot
cd 4st_userbot
chmod +x start-termux.sh
./start-termux.sh
```

To stop: `Ctrl+C`. To start again later: `cd ~/4st_userbot && ./start-termux.sh`.

Optional AI add-on (run only after the bot is installed):

```bash
cd ~/4st_userbot
pip install -r requirements-termux-ai.txt
```

### Deploy to Heroku

[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://heroku.com/deploy?template=https://github.com/oyevipthoma88/4st_userbot)

### Environment Variables

Set these as Heroku Config Vars:

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_API_ID` | ✅ | From https://my.telegram.org |
| `TELEGRAM_API_HASH` | ✅ | From https://my.telegram.org |
| `TELEGRAM_BOT_TOKEN` | ✅ | From @BotFather |
| `TELEGRAM_PRIMARY_SESSION` | ✅ | Telethon session string for owner account |
| `TELEGRAM_OWNER_ID` | ✅ | Your numeric Telegram user ID |
| `TELEGRAM_OWNER_USERNAME` | ❌ | Your Telegram @username (without @) |
| `TELEGRAM_LOG_CHANNEL_ID` | ❌ | Numeric ID of log channel/group |
| `TELEGRAM_HELP_LINK` | ❌ | t.me link for Help/Report button |
| `GEMINI_API_KEY` | ❌ | Google Gemini API key for AI features |
| `GITHUB_TOKEN` | ✅ | PAT (repo scope) for data persistence |
| `GITHUB_REPO` | ✅ | `owner/repo` for data sync (e.g. `yourname/bot-data`) |
| `GITHUB_BRANCH` | ❌ | Branch for data sync (default: `main`) |
| `GITHUB_CONFIG_PATH` | ❌ | Path inside repo for config (default: `data/config.json`) |

> **Tip:** See [`.env.example`](.env.example) for the complete list with descriptions.

Cloud deployments may still use the optional music profile by installing
`requirements.txt` and setting `MUSIC_PUBLIC=1`; the Termux launcher never
installs or starts that profile.

---

## 📦 Stack

- Python 3.11
- Telethon 1.36
- Google Gemini AI
- Heroku (worker dyno)

## 📜 License

MIT License
