@echo off
cd /d "%~dp0"
if "%~1"=="" (
    python "%~dp0setup.py" --usage
    pause
    exit /b
)
python "%~dp0desilence.py" %*
echo.
pause
