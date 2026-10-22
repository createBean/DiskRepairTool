@echo off
chcp 65001 >nul
cd /d "%~dp0"
title NTFS 只读恢复工具

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo 正在申请管理员权限...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

where python >nul 2>&1
if %errorlevel% neq 0 (
  echo.
  echo   没有找到 python
  echo.
  echo   请先安装 Python 3.8 或更高版本：
  echo     https://www.python.org/downloads/
  echo   安装时务必勾选 "Add Python to PATH"
  echo.
  pause
  exit /b 1
)

python -c "import dissect.ntfs" >nul 2>&1
if %errorlevel% neq 0 (
  echo 首次运行，正在安装依赖 dissect.ntfs ...
  python -m pip install dissect.ntfs
  if errorlevel 1 (
    echo.
    echo   依赖安装失败。可以手动执行：
    echo     python -m pip install dissect.ntfs
    echo.
    pause
    exit /b 1
  )
)

echo 启动中，浏览器会自动打开...
python server.py
pause
