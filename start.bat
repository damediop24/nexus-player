@echo off
title Nexus Player
cd /d "%~dp0"
if exist "start-mpv-bridge.vbs" start "" "%~dp0start-mpv-bridge.vbs"
cd backend
echo Starting Nexus Player...
echo (Tip: run "start-bot.bat" in another window for the built-in Telegram downloader bot)
start "" "http://localhost:8899"
python app.py
pause