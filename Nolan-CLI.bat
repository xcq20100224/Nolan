@echo off
rem Nolan console voice mode (legacy): keeps a console window, supports --text.
chcp 65001 >nul
title Nolan
cd /d "%~dp0jarvis"

rem Resolve Python: prefer the interpreter found on PATH (where python),
rem fall back to the bundled managed runtime.
set "NOLAN_PY="
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined NOLAN_PY set "NOLAN_PY=%%P"
)
if not defined NOLAN_PY set "NOLAN_PY=C:\Users\J1896\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\python\.venv\Scripts\python.exe"

"%NOLAN_PY%" jarvis.py %*
