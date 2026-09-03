@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if "%~1"=="" (
    python "%~dp0setup.py" --usage
    pause
    exit /b
)
python "%~dp0desilence.py" %*
echo.
pause
