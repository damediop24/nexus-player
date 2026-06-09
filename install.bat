@echo off
title Nexus Player - Install
cd /d "%~dp0"
echo Installing Python dependencies...
python -m pip install -r requirements.txt
echo.
echo Optional: install FFmpeg and MPV for best experience
echo   winget install Gyan.FFmpeg
echo   winget install shinchiro.mpv
echo.
echo Done! Run start.bat to launch Nexus Player.
pause