@echo off
REM Double-click to run Jhala AI on this laptop (needs .venv and .env). Keep this window open.
cd /d "%~dp0"
start "" http://localhost:8000
.venv\Scripts\python.exe app.py
pause
