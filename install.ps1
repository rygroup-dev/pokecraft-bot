# Pokecraft Bot - installer Windows (Windows 10 / 11)
#
# Jalankan di PowerShell (tidak perlu Administrator):
#   irm https://raw.githubusercontent.com/rygroup-dev/pokecraft-bot/main/install.ps1 | iex
#
# Yang dikerjakan:
#   1. Pasang Python 3.12 kalau belum ada (winget, atau installer resmi python.org)
#   2. Download / update bot ke %USERPROFILE%\pokecraft-bot (.env, data, log tidak disentuh)
#   3. Siapkan virtual environment + library
#   4. Isi .env (private key, token bot Telegram, ID Telegram, kode referral)
#   5. Buat shortcut "Pokecraft Bot" dan "Stop Pokecraft Bot" di Desktop, lalu jalankan bot
#
# Lokasi lain: $env:POKECRAFT_DIR = "D:\pokecraft-bot"; irm ... | iex
# File ini sengaja hanya berisi huruf ASCII (aman untuk PowerShell 5.1).

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'      # Invoke-WebRequest jauh lebih cepat tanpa progress bar
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $Repo    = 'rygroup-dev/pokecraft-bot'
    $RefCode = 'tcswarv7'
    $RefLink = "https://pokecraft.cartridgepad.fun/?ref=$RefCode"
    $Dir     = if ($env:POKECRAFT_DIR) { $env:POKECRAFT_DIR } else { Join-Path $env:USERPROFILE 'pokecraft-bot' }
    $PyVer   = '3.12.7'

    function Say($t)  { Write-Host "[pokecraft] $t" -ForegroundColor Cyan }
    function Ok($t)   { Write-Host "  [ok] $t" -ForegroundColor Green }
    function Warn($t) { Write-Host "  [!] $t" -ForegroundColor Yellow }
    function Fail($t) { Write-Host "  [x] $t" -ForegroundColor Red; throw "STOP" }

    # Python 3.10+ (py launcher, python, atau lokasi install bawaan python.org)
    function Find-Python {
        $cands = @(
            @{ exe = 'py'; pre = @('-3') },
            @{ exe = 'python'; pre = @() },
            @{ exe = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'); pre = @() },
            @{ exe = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'); pre = @() },
            @{ exe = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'); pre = @() }
        )
        # plus every install Windows knows about (python.org / winget register here)
        foreach ($root in 'HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore') {
            if (Test-Path $root) {
                foreach ($k in Get-ChildItem $root) {
                    $ip = Join-Path $k.PSPath 'InstallPath'
                    if (Test-Path $ip) {
                        $exe = (Get-ItemProperty $ip).ExecutablePath
                        if (-not $exe) { $exe = Join-Path (Get-ItemProperty $ip).'(default)' 'python.exe' }
                        if ($exe) { $cands += @{ exe = $exe; pre = @() } }
                    }
                }
            }
        }
        foreach ($c in $cands) {
            try {
                # no quotes in the code: PowerShell 5.1 strips double quotes from native arguments
                $v = & $c.exe @($c.pre + @('-c', 'import sys;print(sys.version_info[0]*100+sys.version_info[1])')) 2>$null
                if ("$v" -match '^\d+$' -and [int]"$v" -ge 310 -and [int]"$v" -lt 400) { return $c }
            } catch { }
        }
        return $null
    }

    function Read-Secret($prompt) {
        $s = Read-Host -Prompt $prompt -AsSecureString
        $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
        try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b).Trim() }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
    }

    try {
        Write-Host ''
        Write-Host '=============================================' -ForegroundColor Magenta
        Write-Host '   Pokecraft Bot - installer Windows' -ForegroundColor Magenta
        Write-Host '=============================================' -ForegroundColor Magenta
        Write-Host "Folder bot : $Dir"
        Write-Host "Referral   : $RefCode"
        Write-Host ''

        # ---------------------------------------------------------------- 1. Python
        Say 'Cek Python 3.10+ ...'
        $py = Find-Python
        if (-not $py) {
            Warn 'Python belum ada, memasang Python 3.12 (khusus user ini, tanpa Administrator) ...'
            $winget = Get-Command winget -ErrorAction SilentlyContinue
            if ($winget) {
                & winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
            }
            $py = Find-Python
            if (-not $py) {
                $arch = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { '' }
                $name = if ($arch) { "python-$PyVer-$arch.exe" } else { "python-$PyVer.exe" }
                $exe  = Join-Path $env:TEMP $name
                Say "Download $name dari python.org ..."
                Invoke-WebRequest -UseBasicParsing "https://www.python.org/ftp/python/$PyVer/$name" -OutFile $exe
                Start-Process -FilePath $exe -ArgumentList '/quiet', 'InstallAllUsers=0', 'PrependPath=1', 'Include_launcher=1', 'Include_test=0' -Wait
                Remove-Item $exe -ErrorAction SilentlyContinue
                $py = Find-Python
            }
            if (-not $py) { Fail 'Python gagal dipasang. Pasang manual dari https://www.python.org/downloads/ (centang "Add python.exe to PATH"), lalu jalankan installer ini lagi.' }
        }
        $pyv = & $py.exe @($py.pre + @('--version'))
        Ok "$pyv"

        # ---------------------------------------------------------------- 2. kode bot
        Say 'Download kode bot ...'
        New-Item -ItemType Directory -Force -Path $Dir | Out-Null
        $git = Get-Command git -ErrorAction SilentlyContinue
        if ($git -and (Test-Path (Join-Path $Dir '.git'))) {
            & git -C $Dir pull --ff-only | Out-Host
        } elseif ($git -and -not (Get-ChildItem $Dir -Force | Select-Object -First 1)) {
            & git clone "https://github.com/$Repo.git" $Dir | Out-Host
        } else {
            # tanpa git: ZIP dari GitHub, ditimpa ke folder (.env, data, logs tidak ada di ZIP jadi aman)
            $zip = Join-Path $env:TEMP 'pokecraft-bot.zip'
            $tmp = Join-Path $env:TEMP 'pokecraft-bot-src'
            Invoke-WebRequest -UseBasicParsing "https://github.com/$Repo/archive/refs/heads/main.zip" -OutFile $zip
            if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
            Expand-Archive -Path $zip -DestinationPath $tmp -Force
            $src = Get-ChildItem $tmp -Directory | Select-Object -First 1
            Copy-Item -Path (Join-Path $src.FullName '*') -Destination $Dir -Recurse -Force
            Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
        if (-not (Test-Path (Join-Path $Dir 'main.py'))) { Fail "Kode bot tidak lengkap di $Dir" }
        Ok "Kode bot ada di $Dir"

        # ---------------------------------------------------------------- 3. venv + library
        Say 'Siapkan virtual environment dan library (1-3 menit) ...'
        $venvPy = Join-Path $Dir '.venv\Scripts\python.exe'
        if (-not (Test-Path $venvPy)) {
            & $py.exe @($py.pre + @('-m', 'venv', (Join-Path $Dir '.venv')))
        }
        if (-not (Test-Path $venvPy)) { Fail 'Gagal membuat virtual environment.' }
        & $venvPy -m pip install --disable-pip-version-check --no-warn-script-location -q --upgrade pip | Out-Host
        & $venvPy -m pip install --disable-pip-version-check --no-warn-script-location -q -r (Join-Path $Dir 'requirements.txt') | Out-Host
        if ($LASTEXITCODE -ne 0) { Fail 'pip install gagal. Cek koneksi internet / proxy, lalu jalankan installer ini lagi.' }
        Ok 'Library terpasang'

        # ---------------------------------------------------------------- 4. akun + .env
        Write-Host ''
        Say 'Akun game'
        Write-Host "  Bot butuh akun Pokecraft yang SUDAH punya save online."
        Write-Host "  Belum punya? Daftar lewat link referral ini:"
        Write-Host "    $RefLink" -ForegroundColor Yellow
        Write-Host "  Buat karakter -> menu Wallet -> connect wallet Solana -> klik 'Save progress online'."
        $has = Read-Host -Prompt '  Sudah punya akun dengan save online? (y/n)'
        if ($has -notmatch '^[yY]') {
            Start-Process $RefLink
            Read-Host -Prompt '  Selesaikan pendaftaran di browser, lalu tekan Enter di sini' | Out-Null
        }

        $envFile = Join-Path $Dir '.env'
        $write = $true
        if (Test-Path $envFile) {
            $keep = Read-Host -Prompt '  File .env sudah ada. Pakai yang lama? (Y/n)'
            if ($keep -notmatch '^[nN]') { $write = $false; Ok '.env lama dipakai' }
        }
        if ($write) {
            Write-Host ''
            Say 'Isi data bot (disimpan HANYA di file .env di komputer ini)'
            Write-Host '  Private key: Phantom -> Settings -> Manage Accounts -> pilih akun -> Show Private Key.'
            Write-Host '  Saran: pakai wallet khusus bot dengan saldo kecil.'
            do {
                $pk = Read-Secret '  PRIVATE_KEY (tidak tampil saat diketik/paste)'
                $okPk = ($pk -match '^[1-9A-HJ-NP-Za-km-z]{43,90}$') -or ($pk -match '^\[\s*\d+(\s*,\s*\d+){63}\s*\]$')
                if (-not $okPk) { Warn 'Format private key tidak dikenal (base58 dari Phantom, atau array 64 angka). Coba lagi.' }
            } until ($okPk)
            Write-Host '  Token bot Telegram: chat @BotFather -> /newbot -> salin token.'
            do {
                $tok = (Read-Host -Prompt '  TELEGRAM_BOT_TOKEN').Trim()
                $okTok = $tok -match '^\d{6,12}:[A-Za-z0-9_-]{30,}$'
                if (-not $okTok) { Warn 'Format token salah (contoh: 123456789:AAH...). Coba lagi.' }
            } until ($okTok)
            Write-Host '  ID Telegram kamu: chat @userinfobot -> angka "Id".'
            do {
                $own = (Read-Host -Prompt '  TELEGRAM_OWNER_ID').Trim()
                $okOwn = $own -match '^\d{4,15}$'
                if (-not $okOwn) { Warn 'ID Telegram berupa angka saja. Coba lagi.' }
            } until ($okOwn)
            $content = @(
                '# Dibuat oleh install.ps1. JANGAN bagikan file ini ke siapa pun.',
                "PRIVATE_KEY=$pk",
                "TELEGRAM_BOT_TOKEN=$tok",
                "TELEGRAM_OWNER_ID=$own",
                "REF_CODE=$RefCode",
                'SOLANA_RPC=',
                ''
            ) -join "`r`n"
            [IO.File]::WriteAllText($envFile, $content, (New-Object Text.UTF8Encoding($false)))
            $pk = $null; $content = $null
            Ok ".env disimpan (referral: $RefCode)"
        }

        # ---------------------------------------------------------------- 5. shortcut + jalankan
        Say 'Buat shortcut di Desktop ...'
        try {
            $desk = [Environment]::GetFolderPath('Desktop')
            $ws = New-Object -ComObject WScript.Shell
            $lnk = $ws.CreateShortcut((Join-Path $desk 'Pokecraft Bot.lnk'))
            $lnk.TargetPath = Join-Path $Dir 'run.bat'
            $lnk.WorkingDirectory = $Dir
            $lnk.Description = 'Jalankan Pokecraft Bot'
            $lnk.Save()
            $lnk = $ws.CreateShortcut((Join-Path $desk 'Stop Pokecraft Bot.lnk'))
            $lnk.TargetPath = Join-Path $Dir 'stop.bat'
            $lnk.WorkingDirectory = $Dir
            $lnk.Description = 'Hentikan Pokecraft Bot dengan aman (game disimpan dulu)'
            $lnk.Save()
            Ok 'Shortcut "Pokecraft Bot" dan "Stop Pokecraft Bot" ada di Desktop'
        } catch { Warn "Shortcut gagal dibuat: $($_.Exception.Message)" }

        $edge = @("${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe", "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
                  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ }
        if ($edge) { Ok 'Browser untuk mode main game: ada' } else { Warn 'Edge/Chrome tidak ditemukan: mode main game mati, farming drop tetap jalan.' }

        Write-Host ''
        Write-Host '=============================================' -ForegroundColor Green
        Write-Host '   Instalasi selesai' -ForegroundColor Green
        Write-Host '=============================================' -ForegroundColor Green
        Write-Host '  Jalankan : klik dua kali "Pokecraft Bot" di Desktop'
        Write-Host '  Berhenti : klik dua kali "Stop Pokecraft Bot" (game disimpan dulu)'
        Write-Host '  Telegram : kirim /menu ke bot kamu'
        Write-Host '  Update   : jalankan perintah installer yang sama lagi (.env tetap)'
        Write-Host "  Log      : $Dir\logs\bot.log"
        Write-Host '  Penting  : jangan minimize jendela game; pakai tombol Sembunyikan di menu Game Telegram.'
        Write-Host ''
        $go = Read-Host -Prompt 'Jalankan bot sekarang? (Y/n)'
        if ($go -notmatch '^[nN]') {
            Start-Process -FilePath 'cmd.exe' -ArgumentList '/k', "title Pokecraft Bot && `"$Dir\run.bat`"" -WorkingDirectory $Dir
            Ok 'Bot dijalankan di jendela baru. Kirim /menu ke bot Telegram kamu.'
        }
    } catch {
        if ($_.Exception.Message -ne 'STOP') { Write-Host "  [x] Error: $($_.Exception.Message)" -ForegroundColor Red }
        Write-Host '  Instalasi berhenti. Perbaiki masalah di atas lalu jalankan perintah installer lagi.' -ForegroundColor Red
    }
}
