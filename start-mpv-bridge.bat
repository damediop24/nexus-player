@echo off
title Nexus MPV Bridge
cd /d "%~dp0"
set "MPV_PATH=C:\mpv\mpv\mpv.exe"
echo Starting Nexus MPV Bridge on port 9340...
echo MPV path: %MPV_PATH%
python "%~dp0mpv_bridge.py" "%MPV_PATH%"