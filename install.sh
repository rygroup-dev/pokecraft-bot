#!/usr/bin/env bash
# Pokecraft Bot - installer VPS (Ubuntu/Debian/Fedora) dan HP Android (Termux).
#
# Jalankan satu baris ini:
#   curl -fsSL https://raw.githubusercontent.com/rygroup-dev/pokecraft-bot/main/install.sh | bash
#
# Yang dikerjakan:
#   1. Pasang paket: git, python3 (+venv), tmux, nano
#   2. Download / update bot ke ~/pokecraft-bot (.env, data, log tidak disentuh)
#   3. Siapkan virtual environment + library
#   4. Isi .env (private key, token bot Telegram, ID Telegram, kode referral)
#   5. Jalankan bot di sesi tmux "poke" (opsional: auto start saat reboot)
#
# Lokasi lain: curl -fsSL ... | POKECRAFT_DIR=/opt/pokecraft-bot bash

main() {
  set -euo pipefail

  REPO="https://github.com/rygroup-dev/pokecraft-bot.git"
  REF_CODE="tcswarv7"
  REF_LINK="https://pokecraft.cartridgepad.fun/?ref=${REF_CODE}"
  DIR="${POKECRAFT_DIR:-$HOME/pokecraft-bot}"
  SESSION="poke"

  c_cyan=$'\033[36m'; c_green=$'\033[32m'; c_yellow=$'\033[33m'; c_red=$'\033[31m'; c_mag=$'\033[35m'; c_off=$'\033[0m'
  say()  { printf '%s[pokecraft]%s %s\n' "$c_cyan" "$c_off" "$*"; }
  ok()   { printf '  %s[ok]%s %s\n' "$c_green" "$c_off" "$*"; }
  warn() { printf '  %s[!]%s %s\n' "$c_yellow" "$c_off" "$*"; }
  die()  { printf '  %s[x]%s %s\n' "$c_red" "$c_off" "$*"; exit 1; }

  # input dari keyboard walau script datang lewat pipa (curl | bash)
  if [ -r /dev/tty ]; then TTY=/dev/tty; else die "Tidak ada terminal untuk mengisi data. Jalankan: bash install.sh"; fi
  ask()    { local v; printf '%s' "$1" > "$TTY"; IFS= read -r v < "$TTY" || true; printf '%s' "$v"; }
  secret() { local v; printf '%s' "$1" > "$TTY"; IFS= read -rs v < "$TTY" || true; printf '\n' > "$TTY"; printf '%s' "$v"; }

  # ------------------------------------------------------------------ platform
  TERMUX=0
  if [ -n "${TERMUX_VERSION:-}" ] || [[ "${PREFIX:-}" == *com.termux* ]]; then TERMUX=1; fi
  SUDO=""
  if [ "$TERMUX" = 0 ] && [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 && SUDO="sudo" || warn "Bukan root dan tidak ada sudo: paket sistem harus sudah terpasang."
  fi

  printf '\n%s=============================================%s\n' "$c_mag" "$c_off"
  printf '%s   Pokecraft Bot - installer %s%s\n' "$c_mag" "$([ "$TERMUX" = 1 ] && echo 'Termux (Android)' || echo 'VPS / Linux')" "$c_off"
  printf '%s=============================================%s\n' "$c_mag" "$c_off"
  echo "Folder bot : $DIR"
  echo "Referral   : $REF_CODE"
  echo

  # ------------------------------------------------------------------ 1. paket
  say "Pasang paket sistem (git, python, tmux, nano) ..."
  if [ "$TERMUX" = 1 ]; then
    pkg update -y -o Dpkg::Options::="--force-confnew" >/dev/null 2>&1 || pkg update -y
    pkg install -y git python tmux nano termux-api >/dev/null 2>&1 || pkg install -y git python tmux nano
  elif command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -y -qq
    DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y -qq git python3 python3-venv python3-pip tmux nano curl ca-certificates
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y -q git python3 python3-pip tmux nano curl
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y -q git python3 python3-pip tmux nano curl
  else
    warn "Package manager tidak dikenal: pastikan git, python3 (3.10+), tmux sudah terpasang."
  fi
  for c in git tmux; do command -v "$c" >/dev/null 2>&1 || die "$c belum terpasang."; done

  PY=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then PY="$c"; break; fi
  done
  [ -n "$PY" ] || die "Butuh Python 3.10 atau lebih baru (VPS: pakai Ubuntu 22.04+ / Debian 12+)."
  ok "$($PY --version)"

  # ------------------------------------------------------------------ 2. kode bot
  say "Download kode bot ..."
  if [ -d "$DIR/.git" ]; then
    git -C "$DIR" pull --ff-only -q
  elif [ -e "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
    die "$DIR sudah ada tapi bukan hasil git clone. Pindahkan/hapus dulu, atau pakai POKECRAFT_DIR lain."
  else
    git clone -q "$REPO" "$DIR"
  fi
  chmod +x "$DIR"/*.sh
  ok "Kode bot ada di $DIR"

  # ------------------------------------------------------------------ 3. venv + library
  say "Siapkan virtual environment dan library (1-3 menit) ..."
  [ -x "$DIR/.venv/bin/python" ] || "$PY" -m venv "$DIR/.venv" || die "Gagal membuat venv (Debian/Ubuntu: apt install python3-venv)."
  "$DIR/.venv/bin/python" -m pip install --disable-pip-version-check -q --upgrade pip
  "$DIR/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$DIR/requirements.txt" || die "pip install gagal. Cek koneksi internet lalu jalankan lagi."
  ok "Library terpasang"

  # ------------------------------------------------------------------ 4. akun + .env
  echo
  say "Akun game"
  echo "  Bot butuh akun Pokecraft yang SUDAH punya save online."
  echo "  Belum punya? Daftar lewat link referral ini (buka di browser HP/PC):"
  printf '    %s%s%s\n' "$c_yellow" "$REF_LINK" "$c_off"
  echo "  Buat karakter -> menu Wallet -> connect wallet Solana -> klik 'Save progress online'."
  has="$(ask '  Sudah punya akun dengan save online? (y/n) ')"
  if [[ ! "$has" =~ ^[yY] ]]; then
    if [ "$TERMUX" = 1 ] && command -v termux-open-url >/dev/null 2>&1; then termux-open-url "$REF_LINK" || true; fi
    ask '  Selesaikan pendaftaran dulu, lalu tekan Enter di sini ' >/dev/null
  fi

  ENV_FILE="$DIR/.env"
  write=1
  if [ -f "$ENV_FILE" ]; then
    keep="$(ask '  File .env sudah ada. Pakai yang lama? (Y/n) ')"
    if [[ ! "$keep" =~ ^[nN] ]]; then write=0; ok ".env lama dipakai"; fi
  fi
  if [ "$write" = 1 ]; then
    echo
    say "Isi data bot (disimpan HANYA di file .env di perangkat ini)"
    echo "  Private key: Phantom -> Settings -> Manage Accounts -> pilih akun -> Show Private Key."
    echo "  Saran: pakai wallet khusus bot dengan saldo kecil."
    while true; do
      PK="$(secret '  PRIVATE_KEY (tidak tampil saat diketik/paste): ')"
      PK="$(printf '%s' "$PK" | tr -d '[:space:]')"
      if [[ "$PK" =~ ^[1-9A-HJ-NP-Za-km-z]{43,90}$ ]] || [[ "$PK" =~ ^\[([0-9]+,){63}[0-9]+\]$ ]]; then break; fi
      warn "Format private key tidak dikenal (base58 dari Phantom, atau array 64 angka). Coba lagi."
    done
    echo "  Token bot Telegram: chat @BotFather -> /newbot -> salin token."
    while true; do
      TOK="$(ask '  TELEGRAM_BOT_TOKEN: ' | tr -d '[:space:]')"
      [[ "$TOK" =~ ^[0-9]{6,12}:[A-Za-z0-9_-]{30,}$ ]] && break
      warn "Format token salah (contoh: 123456789:AAH...). Coba lagi."
    done
    echo "  ID Telegram kamu: chat @userinfobot -> angka 'Id'."
    while true; do
      OWN="$(ask '  TELEGRAM_OWNER_ID: ' | tr -d '[:space:]')"
      [[ "$OWN" =~ ^[0-9]{4,15}$ ]] && break
      warn "ID Telegram berupa angka saja. Coba lagi."
    done
    umask 077
    {
      echo "# Dibuat oleh install.sh. JANGAN bagikan file ini ke siapa pun."
      echo "PRIVATE_KEY=$PK"
      echo "TELEGRAM_BOT_TOKEN=$TOK"
      echo "TELEGRAM_OWNER_ID=$OWN"
      echo "REF_CODE=$REF_CODE"
      echo "SOLANA_RPC="
    } > "$ENV_FILE"
    chmod 600 "$ENV_FILE"
    PK=""
    ok ".env disimpan (izin 600, referral: $REF_CODE)"
  fi

  # ------------------------------------------------------------------ 5. jalankan
  echo
  if command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 || command -v google-chrome >/dev/null 2>&1 || command -v microsoft-edge >/dev/null 2>&1; then
    ok "Browser ditemukan: mode main game bisa dipakai."
  else
    warn "Tidak ada browser Chromium/Chrome: mode main game mati, farming drop + bank + referral tetap jalan."
  fi

  if [ "$TERMUX" = 1 ]; then
    boot="$(ask '  Auto start bot saat HP menyala (butuh app Termux:Boot dari F-Droid)? (y/N) ')"
    if [[ "$boot" =~ ^[yY] ]]; then
      mkdir -p "$HOME/.termux/boot"
      cat > "$HOME/.termux/boot/pokecraft-bot.sh" <<EOF
#!/data/data/com.termux/files/usr/bin/sh
termux-wake-lock
tmux new-session -d -s $SESSION "cd '$DIR' && ./run.sh"
EOF
      chmod +x "$HOME/.termux/boot/pokecraft-bot.sh"
      ok "Auto start terpasang (buka app Termux:Boot sekali supaya aktif)."
    fi
    command -v termux-wake-lock >/dev/null 2>&1 && termux-wake-lock || true
  elif command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    svc="$(ask '  Pasang auto start saat VPS reboot (systemd service)? (y/N) ')"
    if [[ "$svc" =~ ^[yY] ]]; then
      $SUDO tee /etc/systemd/system/pokecraft-bot.service >/dev/null <<EOF
[Unit]
Description=Pokecraft Bot
After=network-online.target
Wants=network-online.target

[Service]
User=$(id -un)
WorkingDirectory=$DIR
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/bin/env tmux new-session -d -s $SESSION "cd '$DIR' && ./run.sh"
# stop.sh asks the bot to save the game and exit; give it time before systemd cleans up
# (run.sh ends after a clean stop, which closes the tmux session)
ExecStop=/bin/sh -c '$DIR/stop.sh; for i in \$\$(seq 1 50); do tmux has-session -t $SESSION 2>/dev/null || exit 0; sleep 1; done'
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
      $SUDO systemctl daemon-reload
      $SUDO systemctl enable pokecraft-bot.service >/dev/null 2>&1
      ok "Service pokecraft-bot aktif saat boot (tetap di tmux '$SESSION')."
    fi
  fi

  if tmux has-session -t "$SESSION" 2>/dev/null; then
    warn "Sesi tmux '$SESSION' sudah jalan. Restart bot: ./stop.sh di $DIR, tunggu 10 detik, lalu jalankan lagi."
  else
    go="$(ask '  Jalankan bot sekarang di tmux? (Y/n) ')"
    if [[ ! "$go" =~ ^[nN] ]]; then
      tmux new-session -d -s "$SESSION" "cd '$DIR' && ./run.sh"
      ok "Bot jalan di tmux '$SESSION'."
    fi
  fi

  printf '\n%s=============================================%s\n' "$c_green" "$c_off"
  printf '%s   Instalasi selesai%s\n' "$c_green" "$c_off"
  printf '%s=============================================%s\n' "$c_green" "$c_off"
  echo "  Lihat bot     : tmux attach -t $SESSION      (keluar tanpa mematikan: Ctrl+B lalu D)"
  echo "  Jalankan      : tmux new -d -s $SESSION 'cd $DIR && ./run.sh'"
  echo "  Berhenti rapi : $DIR/stop.sh"
  echo "  Telegram      : kirim /menu ke bot kamu"
  echo "  Update        : jalankan perintah installer yang sama lagi (.env tetap)"
  echo "  Log           : tail -f $DIR/logs/bot.log"
  if [ "$TERMUX" = 1 ]; then
    echo "  HP Android    : matikan 'battery optimization' untuk Termux di Setelan, supaya bot tidak ditidurkan."
  fi
  echo
}

main "$@"
