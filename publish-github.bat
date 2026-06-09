@echo off
title Publish Nexus Player to GitHub
cd /d "%~dp0"

set GH="C:\Program Files\GitHub CLI\gh.exe"
if not exist %GH% set GH=gh

echo ============================================
echo  Nexus Player - GitHub Publisher
echo  Account: damediop24
echo  Repo:    nexus-player
echo ============================================
echo.

%GH% auth status >nul 2>&1
if errorlevel 1 (
    echo [1/3] GitHub login required - browser will open...
    echo       Sign in as damediop24 and click Authorize.
    echo.
    %GH% auth login -h github.com -p https -w -s repo
    if errorlevel 1 (
        echo Login failed. Run this script again.
        pause
        exit /b 1
    )
)

echo [2/3] Creating repo and pushing...
%GH% repo create nexus-player --public --description "Full-stack media player with yt-dlp" --source=. --remote=origin --push --confirm-rename-to-origin

if errorlevel 1 (
    echo.
    echo Push failed. If repo already exists, trying push only...
    git branch -M main
    git push -u origin main
)

echo.
echo [3/3] Done!
echo.
echo Your repo: https://github.com/damediop24/nexus-player
echo.
pause