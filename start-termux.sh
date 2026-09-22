#!/data/data/com.termux/files/usr/bin/bash
# One-shot Termux launcher: installs non-music deps, then runs the bot.
set -e
cd "$(dirname "$0")"

echo "[1/3] Termux packages..."
pkg update -y >/dev/null 2>&1 || true
pkg install -y python git libjpeg-turbo libexpat openssl

echo "[2/3] Python packages (Termux non-music profile)..."
pip install --upgrade pip wheel
pip install -r requirements-termux.txt

echo "[3/3] Starting bot..."
exec python main.py
