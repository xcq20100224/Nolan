@echo off
rem Nolan Web launcher: start Python backend + Vite frontend with one command.
rem Double-click this file, then open the URL printed by Vite in Chrome/Edge.
title Nolan Web
cd /d "%~dp0nolan-web"
rem Wait 5s for Vite to come up, then open the system default browser (detached,
rem launched before the blocking dev server below). The Kimi embedded preview
rem window may deny microphone access; Chrome/Edge is required for voice input.
start "" /min cmd /c "timeout /t 5 /nobreak > nul & start "" http://localhost:7100/"

rem Resolve npm: prefer the npm.cmd found on PATH (where npm),
rem fall back to the bundled Node runtime.
set "NOLAN_NPM="
for /f "delims=" %%P in ('where npm 2^>nul') do (
    if not defined NOLAN_NPM set "NOLAN_NPM=%%P"
)
if not defined NOLAN_NPM set "NOLAN_NPM=C:\Users\J1896\AppData\Local\Programs\kimi-desktop\resources\resources\runtime\node\npm.cmd"

"%NOLAN_NPM%" run dev
pause
