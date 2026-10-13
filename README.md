# 🎮 Pokecraft Bot

Bot farming untuk [Pokecraft](https://pokecraft.cartridgepad.fun/?ref=tcswarv7) (game Solana), dikontrol penuh dari Telegram.

## Fitur
- 🎣 **Auto farming drop**: mancing, tambang, dan tebang di 7 spot. Hasilnya item tradable dan gold bank. Jeda roll mengikuti cooldown server. Pilihan mode:
  - 🧠 **Pintar**: coba semua spot dulu, lalu fokus ke spot yang hasilnya terbanyak
  - rotasi semua spot
  - satu spot tertentu
- 🏦 **Bank**: auto deposit gold kantong → bank, dengan sisa kantong yang bisa diatur. Bisa juga tarik gold ke kantong.
- 🎮 **Main game sungguhan** (butuh browser Edge/Chrome terbuka): bot memainkan game asli dengan input keyboard & mouse:
  - journey/tutorial, ambil & selesaikan quest, papan quest harian
  - duel trainer & creature liar, befriend, heal di nurse
  - mancing, tangkap serangga, gali, bertani (cangkul, tanam, siram, panen)
  - crafting di tangan/workbench/furnace sesuai resep game, tambang pasir & batu, tebang pohon
  - bangun rumah & ladang untuk quest bangunan
  - jual ke toko, simpan di vault bank, donasi museum, deposit gold lewat Wallet
- 🛒 **Market**: cek harga termurah, jual item / gold dalam POKECRAFT, dan batalkan listing. Ada opsi auto-jual. *Saat ini market ditutup oleh server game untuk token ini; bot mengecek ulang otomatis tiap 30 menit.*
- 🤝 **Referral**: kode & link undangan, progres tugas, dan auto klaim hadiah.
- 🔔 **Notif drop rare** ke Telegram.
- 📊 **Dashboard**: saldo SOL & POKECRAFT, gold, sisa limit harian, hasil hari ini, dan log.
- 🔒 **1 komputer = 1 bot = 1 akun**. Bot hanya merespons pemiliknya.

## Persiapan (sekali saja)
1. **Buat akun lewat link referral**: buka https://pokecraft.cartridgepad.fun/?ref=tcswarv7, buat karakter, lalu di menu **Wallet** connect wallet Solana dan klik **Save progress online**.
2. Buat bot Telegram di [@BotFather](https://t.me/BotFather) (`/newbot`) dan catat tokennya. ID Telegram kamu bisa dilihat di [@userinfobot](https://t.me/userinfobot).
3. Siapkan **private key** wallet tadi (Phantom → Settings → Manage Accounts → pilih akun → Show Private Key). Format base58 atau array 64 angka (Solflare / Solana CLI) sama-sama bisa.

## ⚡ Install satu baris (disarankan)
Installer memasang semua yang dibutuhkan, menanyakan private key, token bot dan ID Telegram, lalu mengisi `.env` (kode referral `tcswarv7` sudah otomatis). Jalankan perintah yang sama lagi untuk **update**; `.env` dan progres tidak hilang.

### 🪟 Windows 10 / 11
Buka **PowerShell** (tekan tombol Windows, ketik `powershell`, Enter; tidak perlu Administrator), lalu tempel:
```powershell
irm https://raw.githubusercontent.com/rygroup-dev/pokecraft-bot/main/install.ps1 | iex
```
- Python 3.12 dipasang otomatis kalau belum ada.
- Bot dipasang ke `C:\Users\<nama>\pokecraft-bot`, plus shortcut **Pokecraft Bot** dan **Stop Pokecraft Bot** di Desktop.
- Lokasi lain: `$env:POKECRAFT_DIR="D:\pokecraft-bot"; irm https://raw.githubusercontent.com/rygroup-dev/pokecraft-bot/main/install.ps1 | iex`

### 🐧 VPS (Ubuntu 22.04+ / Debian 12+ / Fedora)
Login SSH, lalu:
```bash
curl -fsSL https://raw.githubusercontent.com/rygroup-dev/pokecraft-bot/main/install.sh | bash
```
- Paket `git python3 python3-venv tmux nano` dipasang otomatis (pakai `sudo` kalau bukan root).
- Bot jalan di sesi tmux `poke`. Opsional: auto start saat VPS reboot (systemd service `pokecraft-bot`).
- Installer menawarkan pasang browser untuk mode main game (Debian/Fedora: Chromium, Ubuntu: Google Chrome). VPS tanpa layar: game jalan **headless** (tak terlihat) dengan grafik paling ringan, karena VPS tidak punya GPU. Jalannya lebih lambat dari di PC, tapi tetap main.

### 📱 HP Android (Termux + tmux)
1. Install **Termux** dari [F-Droid](https://f-droid.org/packages/com.termux/) (versi Play Store sudah usang). Opsional: **Termux:Boot** dari F-Droid untuk auto start saat HP menyala.
2. Buka Termux, tempel:
```bash
pkg install -y curl && curl -fsSL https://raw.githubusercontent.com/rygroup-dev/pokecraft-bot/main/install.sh | bash
```
3. Di Setelan Android, matikan **battery optimization** untuk Termux supaya bot tidak ditidurkan.

### Perintah harian (VPS & Termux)
| Perlu apa | Perintah |
|---|---|
| Lihat bot jalan | `tmux attach -t poke` (keluar tanpa mematikan: `Ctrl+B` lalu `D`) |
| Jalankan bot | `tmux new -d -s poke 'cd ~/pokecraft-bot && ./run.sh'` |
| Berhenti rapi | `~/pokecraft-bot/stop.sh` |
| Lihat log | `tail -f ~/pokecraft-bot/logs/bot.log` |
| Ganti data `.env` | `nano ~/pokecraft-bot/.env` lalu restart bot |
| Service VPS (kalau dipasang) | `sudo systemctl start` / `stop` / `status pokecraft-bot` |

Setelah bot jalan, kirim **/menu** ke bot Telegram kamu.

## 🛠 Install manual

### 🪟 Windows
1. Install [Python 3.11+](https://www.python.org/downloads/) dan centang **Add python.exe to PATH**.
2. Download repo ini (Code → Download ZIP), lalu extract.
3. Klik dua kali `run.bat`. Pertama kali jalan, file `.env` terbuka di Notepad: isi, simpan, lalu jalankan `run.bat` lagi.
4. Kirim `/menu` ke bot Telegram kamu.

### 🐧 VPS (Ubuntu/Debian)
```bash
sudo apt update && sudo apt install -y git python3 python3-venv tmux
git clone https://github.com/rygroup-dev/pokecraft-bot.git && cd pokecraft-bot
./run.sh            # pertama kali: membuat .env
nano .env           # isi, simpan (Ctrl+O, Enter, Ctrl+X)
tmux new -s poke    # biar tetap jalan saat SSH ditutup
./run.sh            # keluar tmux tanpa mematikan bot: Ctrl+B lalu D
```
Masuk lagi ke bot: `tmux attach -t poke`.

### 📱 HP Android (Termux)
1. Install **Termux** dari [F-Droid](https://f-droid.org/packages/com.termux/). Versi Play Store sudah usang.
2. Jalankan:
```bash
pkg update -y && pkg install -y git python tmux nano
git clone https://github.com/rygroup-dev/pokecraft-bot.git && cd pokecraft-bot
chmod +x run.sh && ./run.sh   # pertama kali: membuat .env
nano .env
termux-wake-lock              # supaya Android tidak menidurkan Termux
tmux new -s poke
./run.sh
```
3. Matikan *battery optimization* untuk Termux di setelan Android.

> ⚠️ Pakai wallet khusus bot dengan saldo kecil. **Jangan pernah membagikan file `.env`.**

## 🎮 Mode main game (browser)
- Nyala/mati dari Telegram: menu **🎮 Game**. Browser (Edge atau Chrome) dibuka otomatis dan login sendiri pakai private key.
- **Jangan minimize** jendela game (game berhenti). Pakai tombol **🙈 Sembunyikan** di menu 🎮 Game.
- Berhenti dengan aman: `stop.bat` (Windows) atau `./stop.sh`. Progres disimpan online dulu sebelum browser ditutup.
- Pause sementara (misalnya mau main sendiri sebentar): buat file `data/pause.flag`, hapus lagi untuk lanjut.
- **Windows**: Edge/Chrome terlihat di layar (pakai GPU, paling lancar). Tombol **🙈 Sembunyikan** memindahkan jendela ke luar layar supaya PC tetap bisa dipakai.
- **VPS tanpa layar**: browser jalan headless otomatis. Tanpa GPU, game pakai render software (SwiftShader) + grafik paling ringan (`?quality=low`, tanpa bayangan; setting di save tidak diubah), sekitar 2-5 fps.
- **VPS dengan desktop (VNC/Xvfb)**: set `DISPLAY` (misal `Environment=DISPLAY=:1` di service) supaya game tampil dan bisa ditonton lewat VNC. Jangan aktifkan 🙈 Sembunyikan di sini.
- **HP Android (Termux)**: tidak ada Chromium, jadi mode game mati otomatis. Farming drop, bank, dan referral tetap jalan tanpa browser.

## Catatan
- Limit harian dari server: 2.000 drop, 3 rare, 3.000 deposit gold.
- Selama mode game jalan, hanya game yang mengubah save. Jangan main di browser lain dengan akun yang sama di saat yang sama.
- Bot hanya mengubah save (deposit/klaim) kalau kamu sudah 10 menit tidak main di browser. Kalau kamu main lagi setelah itu, pilih **Load online progress** supaya save tidak bentrok.
- Referral teman baru dihitung setelah teman main sampai Trainer Lv3 dan minimal 5 menit.
- Log ada di `logs\bot.log`.
