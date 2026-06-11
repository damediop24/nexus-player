@echo off
title Nexus Player - Permanent URL (Railway)
echo.
echo Your permanent URL will be provided by Railway after deploy.
echo.
echo Opening Railway...
echo Sign in with GitHub, create project from repo, generate domain.
echo.
start https://railway.app
start notepad "%~dp0PERMANENT-URL.txt"
echo.
echo See PERMANENT-URL.txt for full steps + how to add persistent volume.
pause