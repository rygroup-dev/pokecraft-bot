#!/usr/bin/env bash
# Stop the bot cleanly: the game saves its progress online first, then the bot exits.
cd "$(dirname "$0")"
mkdir -p data
touch data/stop.flag
echo "[stop] Permintaan berhenti dikirim. Bot akan menyimpan game lalu keluar (±10 detik)."
