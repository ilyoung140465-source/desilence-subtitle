@echo off
cd /d "%~dp0"
python "%~dp0updater.py" --rollback
pause
