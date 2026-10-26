@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

call "%~dp0find_python.bat"
if not defined PY goto nopython

%PY% "%~dp0updater.py"
%PY% "%~dp0app.py"
if errorlevel 1 goto crashed
exit /b

:crashed
echo.
echo   The program stopped with an error. Saving details...
%PY% "%~dp0app.py" > "%~dp0error.txt" 2>&1
echo   Saved: error.txt  (send this file for help)
echo.
pause
exit /b

:nopython
echo.
echo   Python is not ready on this PC.
echo   Run  install.bat  first (double-click it).
echo.
pause
