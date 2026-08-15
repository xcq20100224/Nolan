@echo off
rem ===== Nolan 一键启动（软件形态）=====
rem 双击即用：启动后端（自带前端静态托管），自动打开浏览器。
rem 重复双击无副作用——server.py 单实例守护会清理旧进程。
cd /d "%~dp0nolan-web"
start "" /min python -u server.py
rem 等后端真正就绪再开浏览器（最多 30 秒），避免「浏览器开了服务没起」
for /l %%i in (1,1,30) do (
  curl -s -o nul -m 1 http://localhost:7901/api/version && goto ready
  timeout /t 1 /nobreak >nul
)
echo Nolan 启动较慢，请稍等几秒后手动访问 http://localhost:7901
goto done
:ready
start "" http://localhost:7901
:done
