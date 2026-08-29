@echo off
cd /d "%~dp0"
python "%~dp0capcut_export.py" %*
echo.
pause
