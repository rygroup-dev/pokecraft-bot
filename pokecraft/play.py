"""Phase B runner: keeps the real game open in a browser and lets the Brain play it.

Lifecycle: sign in (same wallet as the API bot), open the game with the online save,
press Continue, play. If the browser dies or the page stops answering, start again."""
import asyncio
import json
import logging
import os
import time

from .browser import Browser, find_browser
from .brain import Brain
from .player import Player

log = logging.getLogger("play")


class GameRunner:
    def __init__(self, engine, data_dir):
        self.e = engine
        self.data_dir = data_dir
        self.browser = None
        self.player = None
        self.brain = None
        self.task = None
        self.state = "off"          # off | starting | playing | error
        self.error = ""
        self.started_at = 0
        self.last = {}              # last game snapshot for the dashboard
        self._stop = asyncio.Event()

    @property
    def available(self):
        return find_browser() is not None

    @property
    def running(self):
        return self.task is not None and not self.task.done()

    async def start(self):
        if self.running:
            return
        self._stop = asyncio.Event()
        self.task = asyncio.create_task(self._run(), name="game")
        self.e.store.set("game_on", True)

    async def stop(self):
        self.e.store.set("game_on", False)
        self._stop.set()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._close()
        self.state = "off"

    async def _close(self):
        if self.player and self.browser and self.browser.alive:
            try:   # let the game write its online save first: pause menu > Save now
                await self.player.close_menus()
                await self.browser.tap("Escape")
                await asyncio.sleep(0.8)
                await self.player.click_button(text="Save now", wait=6)
            except Exception:  # noqa: BLE001
                pass
        if self.browser:
            try:
                await self.browser.close()
            except Exception:  # noqa: BLE001
                pass
        self.browser = self.player = self.brain = None

    async def _open(self):
        api = self.e.api
        await api.login()
        async with self.e._save_lock:      # never read the save while the API side writes it
            s = await api.get_save()
        if not s.get("save"):
            raise RuntimeError("akun belum punya save game: main sekali di browser dulu")
        storage = {"mossglow.save.v2": json.dumps(s["save"]), "pokecraft.econ.token": api.token,
                   "pokecraft.econ.rev": json.dumps({"w": api.wallet.address, "rev": s["rev"]}),
                   "pokecraft.econ.auto": "1"}
        b = Browser(os.path.join(self.data_dir, "browser-profile"),
                    headless=bool(self.e.store.get("game_headless")))
        await b.launch()
        await b.open_game(storage)
        self.browser = b
        for _ in range(100):           # the world takes ~1.5 min to build
            try:
                if await b.eval("window.__pk ? __pk.phase : ''") in ("title", "play"):
                    break
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(3)
        else:
            raise RuntimeError("game tidak selesai dimuat")
        b.size = tuple(json.loads(await b.eval("JSON.stringify([innerWidth, innerHeight])")))
        self.player = Player(b)
        await self.player.inject()
        self.brain = Brain(self.player, notify=self.e.say, store=self.e.store)

    async def _run(self):
        fails = 0
        while not self._stop.is_set():
            try:
                self.state, self.error = "starting", ""
                await self._open()
                self.state, self.started_at = "playing", time.time()
                self.e.event("🎮 Game dibuka, bot mulai main")
                fails = 0
                brain_task = asyncio.create_task(self.brain.run(self._stop))
                if self.e.store.get("game_hidden"):
                    await self.browser.set_hidden(True)
                try:
                    while not self._stop.is_set() and not brain_task.done():
                        await asyncio.sleep(15)
                        if not self.browser.alive:
                            raise RuntimeError("browser tertutup")
                        if await self.browser.ensure_not_minimized():
                            # minimized = the game stops drawing and freezes mid-battle
                            self.e.event("🪟 Jendela game di-minimize, dipulihkan otomatis")
                            await self.e.say("🪟 Jendela game jangan di-minimize (game jadi berhenti).\n"
                                             "Pakai tombol <b>🙈 Sembunyikan</b> di menu 🎮 Game kalau mau disembunyikan.")
                        try:
                            self.last = await asyncio.wait_for(self.player.st(), 20)
                        except Exception as ex:  # noqa: BLE001
                            raise RuntimeError(f"game tidak merespons: {ex}") from ex
                finally:
                    brain_task.cancel()
                    try:
                        await brain_task
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as ex:  # noqa: BLE001
                fails += 1
                self.state, self.error = "error", str(ex)[:200]
                log.warning("game runner error (%d): %s", fails, ex)
                self.e.event(f"⚠️ Game: {self.error}")
                await self._close()
                await asyncio.sleep(min(600, 30 * fails))
        await self._close()

    async def set_hidden(self, hidden):
        self.e.store.set("game_hidden", bool(hidden))
        if self.browser and self.browser.alive:
            await self.browser.set_hidden(bool(hidden))

    async def screenshot(self, path):
        if not self.browser or not self.browser.alive:
            return False
        await self.browser.screenshot(path)
        return True
