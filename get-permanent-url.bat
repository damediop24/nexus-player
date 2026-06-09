@echo off
title Nexus Player - Permanent URL
echo.
echo Your permanent URL will be:
echo.
echo   https://nexus-45gb8rj3.onrender.com
echo.
echo Opening Render deploy page...
echo Sign in with GitHub, click Apply, then Deploy.
echo.
start https://render.com/deploy?repo=https://github.com/damediop24/nexus-player
start notepad "%~dp0PERMANENT-URL.txt"
pause