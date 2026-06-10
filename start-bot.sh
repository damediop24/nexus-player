#!/usr/bin/env bash
cd "$(dirname "$0")"

echo "Starting Telegram bot (restricted content downloader)..."
echo "Downloads will land in the shared downloads/ folder and appear in the Nexus Library."

export BOT_DOWNLOAD_DIR="$(pwd)/downloads"

pushd bot >/dev/null
python3 main.py
popd >/dev/null

echo "Bot stopped."
