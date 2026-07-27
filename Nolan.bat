@echo off
rem Nolan GUI launcher: start the tkinter chat window with pythonw (no console).
rem start "" lets this batch window exit immediately, leaving only the Nolan window.
cd /d "%~dp0jarvis"

rem Resolve Python: prefer the interpreter found on PATH (where python),
rem fall back to the bundled managed runtime. GUI mode needs pythonw.exe.
set "NOLAN_PYW="
for /f "delims=" %%P in ('where pythonw 2^>nul') do (
    if not defined NOLAN_PYW set "NOLAN_PYW=%%P"
)
if not defined NOLAN_PYW (
    for /f "delims=" %%P in ('where python 2^>nul') do (
        if not defined NOLAN_PYW set "NOLAN_PYW=%%~dpPpythonw.exe"
    )
)
if not exist "%NOLAN_PYW%" set "NOLAN_PYW=C:\Users\J1896\AppData\Roaming\kimi-desktop\daimon-share\daimon\runtime\python\.venv\Scripts\pythonw.exe"

start "" "%NOLAN_PYW%" nolan_app.py
