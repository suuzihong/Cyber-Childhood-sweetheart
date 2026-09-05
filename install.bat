@echo off
rem ============================================================
rem  角色AI懒人包 · 安装脚本
rem  1) 探测可用的 Python（优先 venv，其次系统 python，其次 ComfyUI 自带）
rem  2) 创建虚拟环境并安装依赖
rem  3) 若没有 config.json，从 config.example.json 复制一份
rem  4) 若没有 data/persona.md，从 persona.example.md 复制一份
rem  5) 提示下一步
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
echo [install] 开始安装...

rem ---- 1. 探测 Python ----
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  echo [install] 未找到 Python。请安装 Python 3.10+ 并勾选 Add to PATH，然后重跑本脚本。
  pause
  exit /b 1
)
echo [install] 使用 Python: %PY%

rem ---- 2. 创建 venv 并装依赖 ----
if not exist ".venv\Scripts\python.exe" (
  echo [install] 创建虚拟环境 .venv ...
  "%PY%" -m venv .venv
  if errorlevel 1 (
    echo [install] venv 创建失败，尝试直接用系统 Python 装依赖...
  )
)
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
  echo [install] 安装依赖到 .venv ...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
) else (
  echo [install] 用系统 Python 安装依赖 ...
  "%PY%" -m pip install -r requirements.txt
)

rem ---- 3. 生成 config.json ----
if not exist "config.json" (
  copy /y "config.example.json" "config.json" >nul
  echo [install] 已生成 config.json，请打开编辑填写 API / QQ 号等。
) else (
  echo [install] config.json 已存在，跳过。
)

rem ---- 4. 生成人设模板 ----
if not exist "data\persona.md" (
  if not exist "data" mkdir data
  copy /y "persona.example.md" "data\persona.md" >nul
  echo [install] 已生成 data\persona.md（角色人设模板，请编辑）。
) else (
  echo [install] data\persona.md 已存在，跳过。
)

echo.
echo ============================================================
echo  安装完成！接下来：
echo   1) 编辑 config.json      —— 填 API 地址/key、你的QQ号
echo   2) 编辑 data\persona.md  —— 填你的角色人设
echo   3) 编辑 data\user.md     —— 填你的画像（可选）
echo   4) 编辑 data\history.md  —— 填你们共同经历（可选）
echo   5) 跑  import_memory.py  —— 导入 user/history 到记忆（可选）
echo   6) 跑  start.bat         —— 启动调度器
echo ============================================================
pause
