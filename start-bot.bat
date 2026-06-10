@echo off
title Nexus + TG Bot (neex)
cd /d "%~dp0"

echo Starting Telegram bot (restricted content downloader)...
echo Downloads will land in the shared "downloads/" folder and appear in the Nexus Library.
echo.

set BOT_DOWNLOAD_DIR=%cd%\downloads

pushd bot
python main.py
popd

echo.
echo Bot stopped.
pause
