@echo off
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 goto nopython
python "%~dp0updater.py"
python "%~dp0app.py"
exit /b
:nopython
echo.
echo   Python not found. Run install.bat first.
echo.
pause
