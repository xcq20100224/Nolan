@echo off
rem ===== Nolan 一键启动（软件形态）=====
rem 双击即用：启动后端（自带前端静态托管），自动打开浏览器。
rem 重复双击无副作用——server.py 单实例守护会清理旧进程。
cd /d "%~dp0nolan-web"
start "" /min python -u server.py
timeout /t 5 /nobreak >nul
start "" http://localhost:7901
