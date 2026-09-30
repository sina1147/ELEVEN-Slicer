@echo off
cd /d "%~dp0"
title ELEVEN Version S1.1.10.0 - Install
py -3 -m pip install --upgrade pip
py -3 -m pip install -r requirements.txt
echo.
echo Installation completed. Now run RUN_ELEVEN_WEBGL.bat
pause
