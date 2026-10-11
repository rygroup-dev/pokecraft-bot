"""Client for the Pokecraft server API (pokecraft.cartridgepad.fun)."""
import asyncio
import base64
import logging
import secrets

import base58
import httpx

from . import ed25519

log = logging.getLogger("api")

BASE = "https://pokecraft.cartridgepad.fun"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")


class ApiError(Exception):
    def __init__(self, code, msg, status=0, extra=None):
        super().__init__(msg)
        self.code = code
        self.msg = msg
        self.status = status
        self.extra = extra or {}

    @property
    def retry_ms(self):
        v = self.extra.get("retryMs")
        return v if isinstance(v, (int, float)) else None


class Wallet:
    def __init__(self, private_key_b58: str):
        raw = base58.b58decode(private_key_b58.strip())
        if len(raw) not in (32, 64):
            raise ValueError("PRIVATE_KEY harus base58 64-byte (format Phantom) atau 32-byte seed")
        self._seed = raw[:32]
        self.address = base58.b58encode(ed25519.public_key(self._seed)).decode()
        if len(raw) == 64 and base58.b58encode(raw[32:]).decode() != self.address:
            raise ValueError("PRIVATE_KEY rusak: public key tidak cocok")

    def sign_b64(self, message: str) -> str:
        return base64.b64encode(ed25519.sign(self._seed, message.encode())).decode()


class PokecraftApi:
    def __init__(self, wallet: Wallet):
        self.wallet = wallet
        self.token = None
        self.invite_key = None
        self._login_lock = asyncio.Lock()
        self.http = httpx.AsyncClient(
            base_url=BASE, timeout=30,
            headers={"User-Agent": UA, "Origin": BASE, "Referer": BASE + "/",
                     "Accept": "application/json"})

    async def close(self):
        await self.http.aclose()

    # ---- low level -------------------------------------------------------
    async def _raw(self, method, path, body=None, auth=True, invite=False, timeout=None):
        headers = {}
        if auth and self.token:
            headers["Authorization"] = "Bearer " + self.token
        if invite and self.invite_key:
            headers["x-invite-key"] = self.invite_key
        try:
            r = await self.http.request(method, path, json=body, headers=headers,
                                        timeout=timeout or 30)
        except httpx.TimeoutException:
            raise ApiError("timeout", "Server tidak menjawab (timeout).")
        except httpx.HTTPError as e:
            raise ApiError("network", f"Jaringan error: {e.__class__.__name__}")
        try:
            data = r.json()
        except ValueError:
            data = None
        if r.status_code >= 400 or not isinstance(data, dict):
            d = data if isinstance(data, dict) else {}
            raise ApiError(d.get("error", "bad_response"), d.get("msg", f"HTTP {r.status_code}"),
                           r.status_code, d)
        return data

    async def request(self, method, path, body=None, invite=False, timeout=None):
        if not self.token:
            await self.login()
        try:
            return await self._raw(method, path, body, invite=invite, timeout=timeout)
        except ApiError as e:
            # Session expired: sign in again once and retry.
            # (invite endpoints also answer "unauthorized" for a bad invite key)
            if e.code == "unauthorized" and (not invite or "invite key" not in e.msg.lower()):
                self.token = None
                await self.login()
                return await self._raw(method, path, body, invite=invite, timeout=timeout)
            raise

    # ---- auth --------------------------------------------------------------
    async def login(self):
        async with self._login_lock:
            if self.token:
                return
            ch = await self._raw("POST", "/api/auth/challenge",
                                 {"wallet": self.wallet.address}, auth=False)
            msg = ch.get("message", "")
            # Same sanity check as the game: only sign a Pokecraft sign-in text.
            if "Pokecraft sign-in" not in msg or self.wallet.address not in msg:
                raise ApiError("bad_response", "Teks sign-in dari server tidak wajar, tidak ditandatangani.")
            v = await self._raw("POST", "/api/auth/verify",
                                {"wallet": self.wallet.address, "nonce": ch["nonce"],
                                 "signature": self.wallet.sign_b64(msg)}, auth=False)
            self.token = v["token"]
            log.info("LOGIN ok wallet=%s", self.wallet.address)

    # ---- account / save ----------------------------------------------------
    async def me(self):
        return await self.request("GET", "/api/me")

    async def get_save(self):
        return await self.request("GET", "/api/save")

    async def put_save(self, rev, save):
        return await self.request("PUT", "/api/save", {"rev": rev, "save": save}, timeout=25)

    async def deposit(self, gold: int):
        return await self.request("POST", "/api/bank/deposit", {"gold": gold})

    async def withdraw(self, gold: int):
        return await self.request("POST", "/api/bank/withdraw", {"gold": gold})

    # ---- drops ---------------------------------------------------------------
    async def roll(self, activity: str, spot: str):
        body = {"activity": activity, "spot": spot, "castId": secrets.token_hex(16)}
        if activity == "fish":
            body["rod"] = 0
        return await self.request("POST", "/api/drop/roll", body)

    async def use_item(self, item_id: str):
        return await self.request("POST", "/api/items/use", {"itemId": item_id})

    # ---- market --------------------------------------------------------------
    async def listings(self, kind="all", sort="new", cursor=None):
        q = f"/api/market/listings?kind={kind}&sort={sort}"
        if cursor:
            q += f"&cursor={cursor}"
        return await self.request("GET", q)

    async def all_listings(self, kind="all", sort="price", pages=6):
        out, cursor = [], None
        for _ in range(pages):
            r = await self.listings(kind, sort, cursor)
            out += r.get("listings", [])
            cursor = r.get("next")
            if not cursor:
                break
        return out

    async def list_item(self, item_id, price_ui: str):
        return await self.request("POST", "/api/market/list",
                                  {"kind": "item", "itemId": item_id, "priceUi": price_ui})

    async def list_gold(self, gold: int, price_ui: str):
        return await self.request("POST", "/api/market/list",
                                  {"kind": "gold", "gold": gold, "priceUi": price_ui})

    async def cancel_listing(self, listing_id):
        return await self.request("POST", "/api/market/cancel", {"listingId": listing_id})

    async def history(self):
        return await self.request("GET", "/api/market/history")

    # ---- invites -------------------------------------------------------------
    async def invites_register(self):
        return await self.request("POST", "/api/invites/register", {}, invite=True)

    async def invites_me(self):
        return await self.request("GET", "/api/invites/me", invite=True)

    async def invites_claim(self, task):
        return await self.request("POST", "/api/invites/claim", {"task": task}, invite=True)

    async def invites_ack(self, task):
        return await self.request("POST", "/api/invites/ack", {"task": task}, invite=True)

    async def invites_share(self, via="copy"):
        return await self.request("POST", "/api/invites/share", {"via": via}, invite=True)

    # ---- tribes --------------------------------------------------------------
    async def tribe_me(self):
        return await self.request("GET", "/api/tribes/me")

    async def tribes(self):
        return await self.request("GET", "/api/tribes")
