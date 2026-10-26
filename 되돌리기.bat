@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
call "%~dp0find_python.bat"
if not defined PY (echo   Run install.bat first. & pause & exit /b)
%PY% "%~dp0updater.py" --rollback
pause
