@echo off
cd /d "%~dp0"
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set LOG=%~dp0desilence_diag.txt
echo Collecting... please wait.
(
echo ==== computer ====
ver
echo COMPUTERNAME=%COMPUTERNAME%
echo ==== python ====
where python
python -V
echo ==== folder ====
cd
dir /b
echo ==== VERSION ====
type VERSION
echo ==== ffmpeg ====
where ffmpeg
echo ==== packages ====
python -c "import numpy, faster_whisper; print('numpy', numpy.__version__); print('faster_whisper ok')"
echo ==== gpu ====
nvidia-smi --query-gpu=name,driver_version --format=csv
echo ==== updater ====
python "%~dp0updater.py"
echo ==== app import ====
python -c "import app; print('app ok', app.VERSION)"
echo ==== aligner ====
python -c "import aligner; print('gpu:', aligner.available(), aligner.pick_device('auto','auto'))"
) > "%LOG%" 2>&1
echo.
echo   Saved next to this file: desilence_diag.txt
echo   Send that file.
echo.
notepad "%LOG%"
