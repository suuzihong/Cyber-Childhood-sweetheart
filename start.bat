@echo off
rem ============================================================
rem  角色AI懒人包 · 启动脚本
rem  1) 优先 .venv\Scripts\python.exe，其次系统 python
rem  2) 等待 OneBot 网关(默认 ws://127.0.0.1:3001) 就绪，最多等 60 秒
rem  3) 以 --run 启动调度器，日志写 data\scheduler.log
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

rem ---- 1. 探测 Python ----
set "PY="
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo [start] 未找到 Python。先跑 install.bat 安装环境。
  pause
  exit /b 1
)

rem ---- 2. 等待 OneBot 网关就绪（可改端口或跳过）----
set "PORT=3001"
echo [start] 等待 OneBot ws 端口 %PORT% 就绪（最多 60 秒）...
set /a tries=0
:waitloop
powershell -NoProfile -Command "if (Get-NetTCPConnection -LocalPort %PORT% -State Listen -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }" >nul 2>&1
if %errorlevel%==0 goto ready
set /a tries+=1
if %tries% geq 20 goto ready
timeout /t 3 /nobreak >nul
goto waitloop
:ready

rem ---- 3. 启动 ----
if not exist "data" mkdir data
set PYTHONUTF8=1
if not exist "config.json" (
  echo [start] 缺少 config.json！先复制 config.example.json 为 config.json 并填写。
  pause
  exit /b 1
)
"%PY%" -u -X utf8 main.py --config config.json --run >> "data\scheduler.log" 2>&1
