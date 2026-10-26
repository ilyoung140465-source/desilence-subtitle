@echo off

REM Pick a Python that ACTUALLY RUNS.

REM "where python" also finds the Microsoft Store placeholder, which only

REM prints a message and installs nothing - that is why setup kept failing.

set "PY="



python -c "import sys" >nul 2>&1

if not errorlevel 1 (set "PY=python" & goto :eof)



py -3 -c "import sys" >nul 2>&1

if not errorlevel 1 (set "PY=py -3" & goto :eof)



REM Just installed? PATH in this window is stale - look in the usual places.

for %%V in (313 312 311 314) do (

  call :try "%LOCALAPPDATA%\Programs\Python\Python%%V\python.exe"

  if defined PY goto :eof

  call :try "%ProgramFiles%\Python%%V\python.exe"

  if defined PY goto :eof

)

call :try "%LOCALAPPDATA%\Programs\Python\Launcher\py.exe"

if defined PY goto :eof

call :try "%WINDIR%\py.exe"

goto :eof



:try

if not exist %1 goto :eof

%1 -c "import sys" >nul 2>&1

if not errorlevel 1 set PY="%~1"

goto :eof

