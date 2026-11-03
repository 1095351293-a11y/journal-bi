@echo off
title Bank Journal BI
cd /d "%~dp0"

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

if not exist "data\journal.db" (
  echo [init] 未找到账本，正在生成演示数据...
  "%PY%" tools\gen_demo_data.py
)

echo ============================================
echo   Bank Journal BI   http://127.0.0.1:8765
echo   关闭本窗口即停止。数据保存在 data\ 目录。
echo ============================================
"%PY%" app.py
echo.
echo 服务已停止，按任意键关闭窗口。
pause >nul
