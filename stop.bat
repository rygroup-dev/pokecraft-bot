@echo off
REM Stop the bot cleanly: the game saves its progress online first, then the bot exits.
cd /d "%~dp0"
if not exist data mkdir data
type nul > data\stop.flag
echo [stop] Permintaan berhenti dikirim. Bot akan menyimpan game lalu keluar (±10 detik).
