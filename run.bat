@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动校园自习座位预约系统...
python server.py
if errorlevel 1 (
  echo.
  echo 启动失败：请确认已安装 Python 3.9+ 并已加入 PATH。
  pause
)
