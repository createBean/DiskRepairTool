@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包成 exe

echo ================================================
echo  用 PyInstaller 打包成单文件 exe
echo ================================================
echo.
echo 需要先装依赖：
echo   pip install pyinstaller
echo.
pause

python -m pip install --upgrade pyinstaller
if errorlevel 1 goto fail

python -m PyInstaller ^
  --noconfirm ^
  --onefile ^
  --add-data "web;web" ^
  --name "NTFS恢复工具" ^
  --hidden-import core ^
  server.py
if errorlevel 1 goto fail

echo.
echo 打包完成：dist\NTFS恢复工具.exe
echo.
echo 注意：
echo   1. 首次运行 Windows Defender 可能拦截，需要手动允许
echo   2. 程序必须用管理员身份运行，否则读不到原始设备
echo   3. 单文件 exe 启动时会先解压到临时目录，慢几秒是正常的
echo.
pause
exit /b 0

:fail
echo.
echo 打包失败，请检查上面的报错信息。
pause
exit /b 1
