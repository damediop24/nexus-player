@echo off
title Nexus Player
cd /d "%~dp0"
if exist "start-mpv-bridge.vbs" start "" "%~dp0start-mpv-bridge.vbs"
cd backend
echo Starting Nexus Player...
start "" "http://localhost:8899"
python app.py
pause