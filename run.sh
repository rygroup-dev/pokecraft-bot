#!/usr/bin/env bash
# Pokecraft bot launcher for Linux VPS and Android Termux.
set -e
cd "$(dirname "$0")"

PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "[error] Python 3 belum terpasang."
  echo "        VPS (Debian/Ubuntu): sudo apt install -y python3 python3-venv"
  echo "        Termux:              pkg install -y python"
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "[setup] Membuat virtual environment..."
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "[setup] File .env dibuat. Isi PRIVATE_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID:"
  echo "        nano .env"
  echo "        lalu jalankan lagi: ./run.sh"
  exit 1
fi

while true; do
  echo "[run] Pokecraft bot jalan... (./stop.sh untuk berhenti rapi)"
  set +e
  .venv/bin/python main.py
  code=$?
  set -e
  # 0 = stopped on purpose, 3 = another bot already runs on this machine: do not restart
  if [ "$code" = "0" ] || [ "$code" = "3" ]; then break; fi
  echo "[restart] Bot keluar karena error (kode $code), jalan lagi 15 detik lagi. Log: logs/bot.log"
  sleep 15
done
