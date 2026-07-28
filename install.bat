@echo off
chcp 65001 >nul
rem ============================================================
rem  Nolan 环境一键安装脚本（Windows 小白友好版）
rem  双击运行即可：自动建虚拟环境、装 Python/前端依赖、生成配置文件
rem ============================================================
setlocal
cd /d "%~dp0"
title Nolan 环境一键安装

echo ============================================
echo    Nolan 环境一键安装（Windows）
echo ============================================
echo.

rem ===== 第 1 步：检测 Python（先试 py -3，再试 python）=====
echo [1/4] 正在检测 Python ...
set "PY="
py -3 --version >nul 2>&1 && set "PY=py -3"
if not defined PY (
    python --version >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo [错误] 没有找到 Python。
    echo 请访问 https://www.python.org/downloads/ 下载安装 Python 3.10 或更高版本，
    echo 安装时务必勾选 "Add python.exe to PATH"，装完后重新双击本脚本。
    echo.
    pause
    exit /b 1
)
echo        已找到 Python：
%PY% --version

rem ===== 第 2 步：创建 .venv 虚拟环境并安装 Python 依赖 =====
echo.
echo [2/4] 正在创建虚拟环境 .venv 并安装 Python 依赖 ...
if not exist ".venv\Scripts\python.exe" (
    %PY% -m venv .venv
    if errorlevel 1 (
        echo.
        echo [错误] 虚拟环境创建失败。
        echo 建议：检查杀毒软件是否拦截，或右键本脚本选择"以管理员身份运行"重试。
        echo.
        pause
        exit /b 1
    )
) else (
    echo        .venv 已存在，跳过创建。
)
echo        首次安装依赖较慢（语音识别组件较大），请耐心等待 ...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [错误] Python 依赖安装失败，多为网络问题。
    echo 建议：换国内镜像后重新双击本脚本，命令如下——
    echo   .venv\Scripts\python.exe -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)
echo        Python 依赖安装完成。

rem ===== 第 3 步：检测 Node.js 并安装前端依赖 =====
echo.
echo [3/4] 正在检测 Node.js ...
node -v >nul 2>&1
if errorlevel 1 (
    echo.
    echo [错误] 没有找到 Node.js。
    echo 请访问 https://nodejs.org 下载安装 LTS 版本（一路下一步即可），
    echo 装完后重新双击本脚本。
    echo.
    pause
    exit /b 1
)
echo        已找到 Node.js：
node -v
echo        正在安装网页版前端依赖（nolan-web）...
pushd nolan-web
call npm.cmd install
if errorlevel 1 (
    popd
    echo.
    echo [错误] 前端依赖安装失败。
    echo 建议：删除 nolan-web\node_modules 文件夹后重跑本脚本；
    echo 或换国内镜像：npm.cmd config set registry https://registry.npmmirror.com
    echo.
    pause
    exit /b 1
)
popd
echo        前端依赖安装完成。

rem ===== 第 4 步：生成智谱 API 配置文件 =====
echo.
echo [4/4] 正在准备配置文件 ...
if not exist "jarvis\llm_config.json" (
    copy /y "jarvis\llm_config.json.example" "jarvis\llm_config.json" >nul
    echo        已生成 jarvis\llm_config.json
    echo.
    echo  ★ 还差最后一步：用记事本打开 jarvis\llm_config.json，
    echo    把 api_key 改成你的智谱 API Key。
    echo    免费申请地址：https://open.bigmodel.cn
) else (
    echo        jarvis\llm_config.json 已存在，跳过。
)

echo.
echo ============================================
echo    安装完成！
echo    双击 Nolan-Web.bat 启动网页版，
echo    浏览器打开 http://localhost:7100 即可使用。
echo ============================================
echo.
pause
