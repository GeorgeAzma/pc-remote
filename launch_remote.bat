@echo off
REM Launcher for the PC Remote server (see README.md).
REM Runs headless (no console window) using pythonw; errors go to server.log.
REM Prefers the venv interpreter if present, else falls back to global pythonw.
REM
REM Sign-in is built in (Controls -> About -> Sign-in & devices). Optional
REM extra: a fixed token, which also lets a device in via /?token=YOUR_TOKEN
set PC_API_TOKEN=
set PC_API_HOST=0.0.0.0
set PC_API_PORT=1024
REM Screen streaming needs ffmpeg (winget install Gyan.FFmpeg). It is found
REM automatically; set PC_FFMPEG=C:\path\to\ffmpeg.exe to override.

cd /d "%~dp0"
if exist "%~dp0.venv\Scripts\pythonw.exe" (
    "%~dp0.venv\Scripts\pythonw.exe" "%~dp0main.py"
) else (
    pythonw "%~dp0main.py"
)
