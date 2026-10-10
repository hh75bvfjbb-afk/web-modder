@echo off
rem Web Modder: first run creates .venv and installs dependencies, then opens the browser.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  py -3 -m venv .venv 2>nul || python -m venv .venv
  .venv\Scripts\python.exe -m pip install -q --upgrade pip
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt
)
.venv\Scripts\python.exe run.py %*
pause
