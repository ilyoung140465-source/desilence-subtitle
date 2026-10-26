@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
call "%~dp0find_python.bat"
if not defined PY (echo   Run install.bat first. & pause & exit /b)
if "%~1"=="" (
    %PY% "%~dp0setup.py" --usage
    pause
    exit /b
)
%PY% "%~dp0desilence.py" %*
echo.
pause
