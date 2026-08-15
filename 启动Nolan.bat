@echo off
rem ===== Nolan one-click launcher (standalone app mode) =====
rem Double-click to use: starts backend (serves frontend statics too), opens browser.
rem Safe to double-click repeatedly - server.py single-instance guard cleans old ones.
setlocal
cd /d "%~dp0nolan-web"
rem Prefer repo .venv (deps verified), fallback to PATH python
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
echo [Nolan] Python: %PY%
start "" /min "%PY%" -u server.py
rem Wait until backend is really up (max 30s) before opening browser
rem (use absolute System32 paths: PATH may be polluted by Git Bash etc.)
for /l %%i in (1,1,30) do (
  %SystemRoot%\System32\curl.exe -s -o nul -m 1 http://localhost:7901/api/version && goto ready
  %SystemRoot%\System32\timeout.exe /t 1 /nobreak >nul
)
echo [Nolan] Not ready within 30s. Please screenshot this window and send to developer.
pause
goto done
:ready
start "" http://localhost:7901
:done
endlocal
