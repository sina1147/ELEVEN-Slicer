@echo off
cd /d "%~dp0"
title ELEVEN Version S1.1.10.0 - Port 18231
py -3 start_eleven.py
if errorlevel 1 (
  echo.
  echo Python 3.10 or newer is required.
  pause
)
