@echo off
REM Launcher for the PC Remote Control server.
REM Runs headless (no console window) using pythonw.
REM Prefers the venv interpreter if present, else falls back to global pythonw.
REM
REM To start automatically at Windows login:
REM   1. Press Win+R, type  shell:startup , press Enter.
REM   2. Copy this .bat file (or a shortcut to it) into that folder.
REM
REM Optional: set a token so only you can control your PC.
set PC_API_TOKEN=
set PC_API_HOST=0.0.0.0
set PC_API_PORT=1024

cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
) else (
    pythonw "%~dp0main.py"
)
