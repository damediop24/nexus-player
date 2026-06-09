@echo off
title Nexus MPV Bridge
cd /d "%~dp0"
set "MPV_PATH=C:\mpv\mpv\mpv.exe"
echo Stopping any old bridge on port 9340...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :9340 ^| findstr LISTENING') do taskkill /F /PID %%a >nul 2>&1
ping 127.0.0.1 -n 2 >nul
echo Starting Nexus MPV Bridge on port 9340...
echo MPV path: %MPV_PATH%
python "%~dp0mpv_bridge.py" "%MPV_PATH%"