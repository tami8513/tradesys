@echo off
cd /d "%~dp0"
start "" venv\Scripts\pythonw.exe scripts\copy_keys_gui.py
