@echo off
cd /d "%~dp0"
where python >nul 2>&1
if errorlevel 1 goto nopython
python "%~dp0setup.py"
pause
exit /b
:nopython
echo.
echo   Python not found. Trying to install it automatically...
echo.
where winget >nul 2>&1
if errorlevel 1 goto manual
winget install --id Python.Python.3.13 -e --accept-source-agreements --accept-package-agreements
echo.
echo   Python installed.
echo   CLOSE THIS WINDOW and run install.bat again.
echo.
pause
exit /b
:manual
echo   Please install Python from https://www.python.org/downloads/
echo   IMPORTANT: check "Add python.exe to PATH" during setup.
echo   Then run install.bat again.
echo.
pause
