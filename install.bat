@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Setup

echo.
echo ============================================================
echo    TTS silence remover + subtitle sync   -   Setup
echo ============================================================
echo.

call "%~dp0find_python.bat"
if defined PY goto haspython

echo   Python is not ready on this PC. Installing it now.
echo   About 3 minutes. Do not close this window.
echo.

set "URL=https://www.python.org/ftp/python/3.13.3/python-3.13.3-amd64.exe"
set "EXE=%TEMP%\python-3.13.3-amd64.exe"

echo   [1/2] Downloading Python  (27 MB)
curl -L --fail -o "%EXE%" "%URL%"
if not exist "%EXE%" goto nodownload

echo   [2/2] Installing Python
"%EXE%" /passive InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
del "%EXE%" >nul 2>&1

call "%~dp0find_python.bat"
if defined PY goto haspython

echo.
echo   Python was installed, but this window still has the old PATH.
echo.
echo   ==^>  CLOSE THIS WINDOW, then double-click install.bat again.
echo.
pause
exit /b

:haspython
echo   Python: %PY%
%PY% -V
echo.
%PY% "%~dp0setup.py"
echo.
pause
exit /b

:nodownload
echo.
echo   Could not download Python. Check the internet connection.
echo.
echo   Manual way:
echo     1. Open  https://www.python.org/downloads/
echo     2. Click the big yellow "Download Python" button
echo     3. Run it and TICK "Add python.exe to PATH"
echo     4. Then double-click install.bat again
echo.
pause
