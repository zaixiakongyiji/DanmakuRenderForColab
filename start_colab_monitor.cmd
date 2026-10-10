@echo off
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
python -u "%~dp0colab_monitor.py" %*
set "monitor_exit=%ERRORLEVEL%"
echo.
echo Monitor exited with code %monitor_exit%.
pause
exit /b %monitor_exit%
