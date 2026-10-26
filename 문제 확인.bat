@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set LOG=%~dp0desilence_diag.txt
echo Collecting... please wait.
call "%~dp0find_python.bat"
(
echo ==== computer ====
ver
echo COMPUTERNAME=%COMPUTERNAME%
echo ==== python ====
echo chosen: [%PY%]
where python
where py
python -c "import sys; print('python runs', sys.version)"
echo ==== folder ====
cd
dir /b
echo ==== VERSION ====
type VERSION
echo ==== ffmpeg ====
where ffmpeg
echo ==== packages ====
%PY% -c "import numpy, faster_whisper; print('numpy', numpy.__version__); print('faster_whisper ok')"
echo ==== gpu ====
nvidia-smi --query-gpu=name,driver_version --format=csv
echo ==== updater ====
%PY% "%~dp0updater.py"
echo ==== app import ====
%PY% -c "import app; print('app ok', app.VERSION)"
echo ==== aligner ====
%PY% -c "import aligner; print('gpu:', aligner.available(), aligner.pick_device('auto','auto'))"
) > "%LOG%" 2>&1
echo.
echo   Saved next to this file: desilence_diag.txt
echo.
notepad "%LOG%"
