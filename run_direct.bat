@echo off
chcp 65001 >nul
REM 远程桌面控制 - 一键启动直连模式
cd /d "%~dp0"
echo 正在安装依赖...
pip install -r requirements.txt
echo.
echo 启动直连模式服务器...
python server.py
pause
