"""Pokecraft bot entry point: farming engine + Telegram dashboard."""
import logging
import os
import socket
import sys
from logging.handlers import RotatingFileHandler

try:
    import truststore

    truststore.inject_into_ssl()  # use the OS certificate store (needed behind re-signing proxies)
except Exception:  # noqa: BLE001 - optional; plain certifi works on most VPS/Termux setups
    pass

from dotenv import load_dotenv  # noqa: E402

from pokecraft.api import PokecraftApi, Wallet  # noqa: E402
from pokecraft.chain import Chain  # noqa: E402
from pokecraft.engine import Engine  # noqa: E402
from pokecraft.store import Store  # noqa: E402
from pokecraft.tg import TelegramUI  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCK_PORT = 47391  # one bot (one account) per machine


def setup_logging():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji in a cp1252 console
    except (AttributeError, ValueError):
        pass
    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(os.path.join(ROOT, "logs", "bot.log"), maxBytes=2_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    for noisy in ("httpx", "telegram", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def single_instance():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        print("[error] Bot Pokecraft sudah jalan di komputer ini. 1 komputer = 1 bot = 1 akun.")
        sys.exit(1)
    return s  # keep the socket open for the whole run


def need(name):
    v = (os.getenv(name) or "").strip()
    if not v:
        print(f"[error] {name} belum diisi di file .env (lihat .env.example)")
        sys.exit(1)
    return v


def main():
    lock = single_instance()  # noqa: F841
    load_dotenv(os.path.join(ROOT, ".env"))
    setup_logging()
    wallet = Wallet(need("PRIVATE_KEY"))
    owner = int(need("TELEGRAM_OWNER_ID"))
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    store = Store(os.path.join(ROOT, "data", "state.json"))
    engine = Engine(PokecraftApi(wallet), store, Chain(os.getenv("SOLANA_RPC") or None),
                    ref_code=os.getenv("REF_CODE", "tcswarv7"))
    ui = TelegramUI(need("TELEGRAM_BOT_TOKEN"), owner, engine)
    orig = ui.app.post_init

    async def post_init(app):
        await orig(app)
        engine.start()
    ui.app.post_init = post_init
    logging.getLogger("main").info("START wallet=%s", wallet.address)
    ui.run()


if __name__ == "__main__":
    main()
