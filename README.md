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
1. **Buat akun**: buka https://pokecraft.cartridgepad.fun/?ref=tcswarv7, buat karakter, lalu di menu **Wallet** connect wallet Solana dan klik **Save progress online**.
2. Buat bot Telegram di [@BotFather](https://t.me/BotFather) dan catat tokennya. ID Telegram kamu bisa dilihat di [@userinfobot](https://t.me/userinfobot).
3. Siapkan isi `.env`:
   - `PRIVATE_KEY`: private key wallet (Phantom → Settings → Export Private Key)
   - `TELEGRAM_BOT_TOKEN` dan `TELEGRAM_OWNER_ID`

## 🪟 Windows
1. Install [Python 3.11+](https://www.python.org/downloads/) dan centang **Add python.exe to PATH**.
2. Download repo ini (Code → Download ZIP), lalu extract.
3. Klik dua kali `run.bat`. Pertama kali jalan, file `.env` terbuka di Notepad: isi, simpan, lalu jalankan `run.bat` lagi.
4. Kirim `/menu` ke bot Telegram kamu.

## 🐧 VPS (Ubuntu/Debian)
```bash
sudo apt update && sudo apt install -y git python3 python3-venv tmux
git clone https://github.com/rygroup-dev/pokecraft-bot.git && cd pokecraft-bot
./run.sh            # pertama kali: membuat .env
nano .env           # isi, simpan (Ctrl+O, Enter, Ctrl+X)
tmux new -s poke    # biar tetap jalan saat SSH ditutup
./run.sh            # keluar tmux tanpa mematikan bot: Ctrl+B lalu D
```
Masuk lagi ke bot: `tmux attach -t poke`.

## 📱 HP Android (Termux)
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
- Di VPS/HP tanpa layar, matikan mode game di menu 🎮 Game: farming drop, bank, dan referral tetap jalan tanpa browser.

## Catatan
- Limit harian dari server: 2.000 drop, 3 rare, 3.000 deposit gold.
- Selama mode game jalan, hanya game yang mengubah save. Jangan main di browser lain dengan akun yang sama di saat yang sama.
- Bot hanya mengubah save (deposit/klaim) kalau kamu sudah 10 menit tidak main di browser. Kalau kamu main lagi setelah itu, pilih **Load online progress** supaya save tidak bentrok.
- Referral teman baru dihitung setelah teman main sampai Trainer Lv3 dan minimal 5 menit.
- Log ada di `logs\bot.log`.
