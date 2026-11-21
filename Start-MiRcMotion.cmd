@echo off
python "%~dp0mi_rc_motion_windows.py" %*
if errorlevel 1 pause
