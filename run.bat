@echo off
REM Pokecraft bot launcher. Uses portable .runtime\python if present, else .venv.
setlocal
cd /d "%~dp0"

set "PY=%~dp0.runtime\python\python.exe"
if exist "%PY%" goto :deps
set "PY=%~dp0.venv\Scripts\python.exe"
if exist "%PY%" goto :deps
echo [setup] Membuat virtual environment...
where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
if not exist "%PY%" (
    echo [error] Python 3.11+ tidak ditemukan. Install dari https://www.python.org/downloads/
    pause
    exit /b 1
)

:deps
"%PY%" -m pip install --disable-pip-version-check --no-warn-script-location -q -r requirements.txt
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [setup] File .env dibuat. Isi PRIVATE_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID lalu jalankan lagi.
    notepad .env
    pause
    exit /b 1
)

:loop
echo [run] Pokecraft bot jalan... (stop.bat untuk berhenti rapi)
"%PY%" main.py
REM 0 = stopped on purpose, 3 = another bot already runs on this PC: do not restart
if %errorlevel%==0 goto :end
if %errorlevel%==3 goto :end
echo [restart] Bot keluar karena error (kode %errorlevel%), jalan lagi 15 detik lagi. Log: logs\bot.log
timeout /t 15 /nobreak >nul
goto :loop

:end
echo.
echo [stopped] Bot berhenti. Log: logs\bot.log
pause
endlocal
