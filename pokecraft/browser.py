"""Runs the real game in Edge/Chrome and drives it over the DevTools protocol (CDP).

The page is the official client. Two small things are added:
  * a read-only handle `window.__pk` to the game object, so the bot can see positions,
    quests and menus (the same things a player sees on screen);
  * the online save and session token are put into localStorage before the page loads,
    exactly like "Load online progress" after signing in.
Everything the character does goes through real keyboard/mouse input."""
import asyncio
import base64
import itertools
import json
import logging
import os
import shutil
import socket
import subprocess
import sys

import httpx
import websockets

log = logging.getLogger("browser")

SITE = "https://pokecraft.cartridgepad.fun/"
HOOKS = [
    # handle to the game object as soon as the title screen is ready
    # plus the read-only data tables (blocks, species, moves, items, places, shops, recipes, quests)
    ("this.pendingSave=l,", "window.__pk=this,window.__pkx=(()=>{try{return{F,Du,rv,nv,Ag,Kh,ew,kS,aC,o_,jg,sg,z1,_h,tg}}"
                            "catch(e){return{err:String(e)}}})(),"
                            # the game's own rule functions, each on its own so one missing name
                            # does not hide the others: NZ requirement check (shop rows), yC
                            # recipe list, EC can-craft check, dC recipe known, kw build detector
                            "Object.assign(window.__pkx,(()=>{const o={};try{o.NZ=NZ}catch(e){}try{o.yC=yC}catch(e){}"
                            "try{o.EC=EC}catch(e){}try{o.dC=dC}catch(e){}try{o.kw=kw}catch(e){}return o})()),"
                            "this.pendingSave=l,"),
    ("async beginSession(e,t){", "async beginSession(e,t){window.__pk=this;"),
]
KEYS = {  # code -> (key, windowsVirtualKeyCode)
    **{f"Key{c}": (c.lower(), ord(c)) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    **{f"Digit{d}": (str(d), 48 + d) for d in range(10)},
    "Space": (" ", 32), "ShiftLeft": ("Shift", 16), "Enter": ("Enter", 13),
    "Escape": ("Escape", 27), "Tab": ("Tab", 9), "ControlLeft": ("Control", 17),
    "ArrowUp": ("ArrowUp", 38), "ArrowDown": ("ArrowDown", 40),
    "ArrowLeft": ("ArrowLeft", 37), "ArrowRight": ("ArrowRight", 39),
}


def find_browser():
    cands = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                          os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    else:
        cands += [shutil.which(n) or "" for n in ("chromium", "chromium-browser", "google-chrome",
                                                   "microsoft-edge")]
    return next((c for c in cands if c and os.path.exists(c)), None)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class CDPError(Exception):
    pass


class Browser:
    def __init__(self, profile_dir, headless=False, width=1280, height=720):
        self.profile_dir = profile_dir
        self.headless = headless
        self.size = (width, height)
        self.proc = None
        self.ws = None
        self.port = None
        self._ids = itertools.count(1)
        self._pending = {}
        self._handlers = []
        self._reader = None
        self.session_id = None

    # ------------------------------------------------------------------ process
    async def launch(self):
        exe = find_browser()
        if not exe:
            raise RuntimeError("Edge/Chrome tidak ditemukan")
        self.port = free_port()
        os.makedirs(self.profile_dir, exist_ok=True)
        args = [exe, f"--remote-debugging-port={self.port}", f"--user-data-dir={self.profile_dir}",
                f"--window-size={self.size[0]},{self.size[1]}", "--no-first-run",
                "--no-default-browser-check", "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding", "--disable-backgrounding-occluded-windows",
                "--autoplay-policy=no-user-gesture-required", "--mute-audio",
                "--disable-features=CalculateNativeWinOcclusion,Translate", "about:blank"]
        if self.headless:
            args.insert(1, "--headless=new")
            args.insert(1, "--use-angle=swiftshader")
            args.insert(1, "--enable-unsafe-swiftshader")
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = None
        async with httpx.AsyncClient() as c:
            for _ in range(60):
                try:
                    tabs = (await c.get(f"http://127.0.0.1:{self.port}/json")).json()
                    page = next((t for t in tabs if t.get("type") == "page"), None)
                    if page:
                        url = page["webSocketDebuggerUrl"]
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.5)
        if not url:
            raise RuntimeError("browser tidak membuka port debug")
        self.ws = await websockets.connect(url, max_size=64 * 1024 * 1024, ping_interval=None)
        self._reader = asyncio.create_task(self._read_loop())
        log.info("browser up: %s (port %s)", os.path.basename(exe), self.port)

    async def attach(self, port):
        """Connect to an already running browser (debug/dev use)."""
        self.port = port
        async with httpx.AsyncClient() as c:
            tabs = (await c.get(f"http://127.0.0.1:{port}/json")).json()
        page = next(t for t in tabs if t.get("type") == "page" and "pokecraft" in t.get("url", ""))
        self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024,
                                           ping_interval=None)
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self):
        if self._reader:
            self._reader.cancel()
        try:
            if self.ws:
                await self.ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    @property
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # ------------------------------------------------------------------ cdp plumbing
    async def _read_loop(self):
        async for raw in self.ws:
            msg = json.loads(raw)
            if "id" in msg:
                fut = self._pending.pop(msg["id"], None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(CDPError(msg["error"].get("message")))
                    else:
                        fut.set_result(msg.get("result", {}))
            else:
                for h in list(self._handlers):
                    try:
                        r = h(msg)
                        if asyncio.iscoroutine(r):
                            asyncio.create_task(r)
                    except Exception:  # noqa: BLE001
                        log.exception("cdp handler")

    async def send(self, method, params=None, timeout=60):
        i = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        await self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        return await asyncio.wait_for(fut, timeout)

    def on(self, handler):
        self._handlers.append(handler)

    async def eval(self, expr, timeout=30):
        r = await self.send("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                                 "awaitPromise": True}, timeout)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise CDPError((d.get("exception") or {}).get("description") or d.get("text"))
        return (r.get("result") or {}).get("value")

    # ------------------------------------------------------------------ game page
    async def open_game(self, storage: dict):
        """Load the game with the hook patched in and localStorage pre-filled."""
        await self.send("Page.enable")
        await self.send("Runtime.enable")
        seed = json.dumps(storage)
        # No pointer lock: the game then uses its built-in drag-to-look mode (as on devices
        # without pointer lock), which does not pause whenever the window loses focus.
        await self.send("Page.addScriptToEvaluateOnNewDocument", {"source": (
            "try { Object.defineProperty(Element.prototype, 'requestPointerLock',"
            " {value: undefined, configurable: true, writable: true}); } catch (e) {}")})
        await self.send("Page.addScriptToEvaluateOnNewDocument", {"source": (
            "if (location.hostname === 'pokecraft.cartridgepad.fun' && !sessionStorage.__pkSeeded) {"
            f" const s = {seed}; for (const k in s) localStorage.setItem(k, s[k]);"
            " sessionStorage.__pkSeeded = '1'; }")})
        await self.send("Fetch.enable", {"patterns": [
            {"urlPattern": SITE + "*", "resourceType": "Document", "requestStage": "Response"}]})
        self.on(self._patch_document)
        await self.send("Page.navigate", {"url": SITE})

    async def _patch_document(self, msg):
        if msg.get("method") != "Fetch.requestPaused":
            return
        p = msg["params"]
        rid = p["requestId"]
        try:
            body = await self.send("Fetch.getResponseBody", {"requestId": rid})
            html = base64.b64decode(body["body"]).decode("utf-8") if body.get("base64Encoded") else body["body"]
            n = 0
            for old, new in HOOKS:
                if old in html:
                    html = html.replace(old, new, 1)
                    n += 1
            log.info("game page patched: %d/%d hooks", n, len(HOOKS))
            headers = [h for h in p.get("responseHeaders", [])
                       if h["name"].lower() not in ("content-length", "content-encoding",
                                                     "content-security-policy")]
            await self.send("Fetch.fulfillRequest", {
                "requestId": rid, "responseCode": p.get("responseStatusCode", 200),
                "responseHeaders": headers,
                "body": base64.b64encode(html.encode("utf-8")).decode()})
        except Exception as e:  # noqa: BLE001
            log.warning("patch failed (%s), page loads unpatched", e)
            try:
                await self.send("Fetch.continueRequest", {"requestId": rid})
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ input
    async def key(self, code, down=True):
        k, vk = KEYS.get(code, (code, 0))
        await self.send("Input.dispatchKeyEvent", {
            "type": "keyDown" if down else "keyUp", "code": code, "key": k,
            "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk,
            **({"text": k} if down and len(k) == 1 else {})})

    async def tap(self, code, hold=0.06):
        await self.key(code, True)
        await asyncio.sleep(hold)
        await self.key(code, False)

    async def click(self, x, y, button="left"):
        for t in ("mousePressed", "mouseReleased"):
            await self.send("Input.dispatchMouseEvent", {"type": t, "x": x, "y": y, "button": button,
                                                         "clickCount": 1})
            await asyncio.sleep(0.05)

    async def drag_look(self, dx, dy, steps=None):
        """Turn the camera like a player dragging the mouse (drag-to-look mode)."""
        cx, cy = self.size[0] // 2, self.size[1] // 2
        steps = steps or max(2, int(max(abs(dx), abs(dy)) // 40) + 1)
        await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": cx, "y": cy})
        await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy,
                                                     "button": "left", "buttons": 1, "clickCount": 1})
        for i in range(1, steps + 1):
            await self.send("Input.dispatchMouseEvent", {
                "type": "mouseMoved", "x": cx + dx * i / steps, "y": cy + dy * i / steps,
                "button": "left", "buttons": 1})
            await asyncio.sleep(0.016)
        await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": cx + dx, "y": cy + dy,
                                                     "button": "left", "buttons": 0, "clickCount": 1})

    async def taps(self, n=8, gap=0.012):
        """Quick clicks on the 3D view. In drag-to-look mode the game counts a block hit
        only while clicks keep coming every frame (hold timer 0.05 s), see act.primary."""
        cx, cy = self.size[0] // 2, self.size[1] // 2
        for _ in range(n):
            await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", "x": cx, "y": cy,
                                                         "button": "left", "buttons": 1, "clickCount": 1})
            await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": cx, "y": cy,
                                                         "button": "left", "buttons": 0, "clickCount": 1})
            await asyncio.sleep(gap)

    async def click_center(self, button="left"):
        """A short click on the 3D view: casts the rod, swings a tool, hits the aimed block."""
        cx, cy = self.size[0] // 2, self.size[1] // 2
        await self.click(cx, cy, button)

    # ------------------------------------------------------------------ window
    # A minimized window stops requestAnimationFrame, which freezes the game (battles hang).
    async def window(self):
        return await self.send("Browser.getWindowForTarget", {})

    async def ensure_not_minimized(self):
        w = await self.window()
        if w["bounds"].get("windowState") == "minimized":
            await self.send("Browser.setWindowBounds", {"windowId": w["windowId"],
                                                        "bounds": {"windowState": "normal"}})
            return True
        return False

    async def set_hidden(self, hidden):
        """Hidden = moved off-screen (still rendering), so the PC stays usable."""
        w = await self.window()
        if w["bounds"].get("windowState") != "normal":
            await self.send("Browser.setWindowBounds", {"windowId": w["windowId"], "bounds": {"windowState": "normal"}})
        left, top = (-4000, 10) if hidden else (10, 10)
        await self.send("Browser.setWindowBounds", {"windowId": w["windowId"],
                                                    "bounds": {"left": left, "top": top}})

    async def fps(self):
        return await self.eval("new Promise(r => { let c = 0; const t0 = performance.now();"
                               " const f = () => { c++; if (performance.now() - t0 < 1000) requestAnimationFrame(f); else r(c); };"
                               " requestAnimationFrame(f); setTimeout(() => r(c), 1500); })")

    async def screenshot(self, path):
        r = await self.send("Page.captureScreenshot", {"format": "png"})
        with open(path, "wb") as f:
            f.write(base64.b64decode(r["data"]))
