@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

where python >nul 2>&1
if errorlevel 1 goto nopython

REM Microsoft Store stub check: it is not a real Python
python -c "import sys" >nul 2>&1
if errorlevel 1 goto storestub

python "%~dp0updater.py"
python "%~dp0app.py"
if errorlevel 1 goto crashed
exit /b

:crashed
echo.
echo   The program stopped with an error.
echo   Saving details to: %~dp0error.txt
echo.
python "%~dp0app.py" > "%~dp0error.txt" 2>&1
echo   Send error.txt to get help.
echo.
pause
exit /b

:storestub
echo.
echo   Windows found only the Microsoft Store placeholder for Python.
echo   Fix: Settings ^> Apps ^> Advanced app settings ^>
echo        App execution aliases ^> turn OFF python.exe and python3.exe
echo   Then run install.bat
echo.
pause
exit /b

:nopython
echo.
echo   Python not found. Run install.bat first.
echo.
pause
