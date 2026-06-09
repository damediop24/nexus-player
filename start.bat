@echo off
title Nexus Player
cd /d "%~dp0backend"
echo Starting Nexus Player...
start "" "http://localhost:8899"
python app.py
pause