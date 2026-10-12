"""Automation engine: drop farming, bank deposit, invite rewards, market selling."""
import asyncio
import logging
import random
import secrets
import time

from .api import ApiError, PokecraftApi
from .chain import Chain
from .store import Store

log = logging.getLogger("engine")

# Server-side drop spots, taken from the game client (j6 / k6 / A6).
SPOTS = {
    "fish:sea": ("fish", "sea", "🌊 Mancing laut"),
    "fish:lake": ("fish", "lake", "🏞 Mancing danau"),
    "fish:river": ("fish", "river", "🏔 Mancing sungai"),
    "mine:mountain": ("mine", "mountain", "⛰ Tambang gunung"),
    "mine:cave": ("mine", "cave", "🕳 Tambang gua"),
    "mine:arctic": ("mine", "arctic", "❄️ Tambang es"),
    "chop:forest": ("chop", "forest", "🌲 Tebang pohon"),
}
RARITY_ORDER = ["common", "uncommon", "rare", "epic", "legendary"]
# Rough value of a drop in "gold units" (100 gold ≈ 1 POKECRAFT, a common item ≈ 10).
# Only used to rank spots against each other in smart mode.
RARITY_VALUE = {"common": 1000, "uncommon": 2500, "rare": 10000, "epic": 30000, "legendary": 100000}
SMART_MIN_ROLLS = 15     # every spot is sampled this often before it can be judged
SMART_EXPLORE = 0.15     # share of rolls still spent on other spots
IDLE_SEC = 600           # the save must be untouched this long before the bot edits it
RARITY_ICON = {"common": "⚪", "uncommon": "🟢", "rare": "🔵", "epic": "🟣", "legendary": "🟡"}
INVITE_TASKS = {
    "share": "Bagikan link undangan",
    "invite_1": "Undang 1 teman",
    "invite_3": "Undang 3 teman",
    "invite_10": "Undang 10 teman",
    "together": "Main bareng teman",
}
MAX_GEMS = 999_999
SITE = "https://pokecraft.cartridgepad.fun"


def rarity_rank(r):
    return RARITY_ORDER.index(r) if r in RARITY_ORDER else 0


class Engine:
    def __init__(self, api: PokecraftApi, store: Store, chain: Chain, ref_code=None):
        self.api = api
        self.store = store
        self.chain = chain
        self.ref_code = (ref_code or "").strip().lower() or None
        self.notify = None              # async callable(text) set by the Telegram layer
        self.game = None                # GameRunner (phase B), set by main
        self.account = None             # /api/me
        self.save_info = {}             # summary of /api/save
        self.invites = None
        self.balances = (None, None)
        self.status = "starting"
        self.next_roll_at = 0
        self.last_error = ""
        self.rotate_i = 0
        # Market health: the server answers 503 market_disabled while it cannot handle the
        # token (seen 2026-10-11: "bad token-2022 mint layout").
        self.market_ok = None
        self.market_msg = ""
        self.market_checked = 0
        self._save_lock = asyncio.Lock()
        self._tasks = []

    # ------------------------------------------------------------------ helpers
    def event(self, text):
        log.info("EVENT %s", text)
        self.store.event(text)

    async def say(self, text):
        if self.notify:
            try:
                await self.notify(text)
            except Exception as e:  # noqa: BLE001
                log.warning("notify failed: %s", e)

    def start(self):
        self._tasks = [asyncio.create_task(self._farm_loop(), name="farm"),
                       asyncio.create_task(self._maint_loop(), name="maint")]

    async def stop(self):
        for t in self._tasks:
            t.cancel()

    # ------------------------------------------------------------------ refresh
    async def refresh(self, full=False):
        self.account = await self.api.me()
        s = await self.api.get_save()
        save = s.get("save")
        self.save_info = {"rev": s.get("rev"), "exists": bool(save)}
        if save:
            flags = save.get("flags", {})
            done = sum(1 for q in save.get("quests", {}).values()
                       if q and (q.get("phase") == "done" or q.get("doneCount", 0) > 0))
            self.save_info.update({
                "name": save.get("name"), "lvl": save.get("lvl"), "xp": save.get("xp"),
                "gems": save.get("gems", 0), "party": save.get("party", []),
                "quests_done": done, "quests_total": len(save.get("quests", {})),
                "dex": len(save.get("dex", {})), "play_sec": save.get("stats", {}).get("playSec", 0),
                "inv_key": flags.get("inv:key"),
            })
            self.api.invite_key = flags.get("inv:key") or self.api.invite_key
        if full:
            cfg = self.account.get("config", {})
            self.balances = await self.chain.balances(self.api.wallet.address, cfg.get("mint", ""),
                                                      cfg.get("decimals", 6))
        return self.account

    @property
    def limits(self):
        return (self.account or {}).get("limits", {})

    @property
    def items(self):
        return (self.account or {}).get("items", [])

    # ------------------------------------------------------------------ farming
    def spot_value(self, key):
        """Average value per roll of a spot, from our own history."""
        st = self.store.data["spots"].get(key, {})
        r = st.get("rolls", 0)
        if not r:
            return 0.0
        v = st.get("gold", 0) + sum(st.get(k, 0) * w for k, w in RARITY_VALUE.items())
        return v / r

    def best_spot(self):
        return max(SPOTS, key=self.spot_value)

    def pick_spot(self):
        mode = self.store.get("spot_mode")
        if mode in SPOTS:
            return mode
        keys = list(SPOTS)
        if mode == "smart":
            fresh = [k for k in keys if self.store.data["spots"].get(k, {}).get("rolls", 0) < SMART_MIN_ROLLS]
            if fresh:
                return fresh[0]
            if random.random() >= SMART_EXPLORE:
                return self.best_spot()
            return random.choice(keys)
        self.rotate_i = (self.rotate_i + 1) % len(keys)
        return keys[self.rotate_i]

    # ------------------------------------------------------------------ save safety
    def game_owns_save(self):
        """While phase B plays the game, the game client is the only writer of the save:
        pouch gold and rewards move through its own Wallet/Invite pages."""
        return self.game is not None and (self.game.running or bool(self.store.get("game_on")))

    def player_idle(self):
        """True when nobody played in the browser recently (save rev and playtime unchanged)."""
        if self.game_owns_save():
            return False
        mark = (self.save_info.get("rev"), self.save_info.get("play_sec"))
        now = time.time()
        if mark != getattr(self, "_save_mark", None):
            first = not hasattr(self, "_save_mark")
            self._save_mark, self._save_mark_at = mark, (now - IDLE_SEC if first else now)
        return now - self._save_mark_at >= IDLE_SEC

    def _own_write(self, rev):
        """Remember the rev we wrote ourselves so it does not count as player activity."""
        self.save_info["rev"] = rev
        self._save_mark = (rev, self.save_info.get("play_sec"))

    async def roll_once(self):
        key = self.pick_spot()
        activity, spot, _ = SPOTS[key]
        r = await self.api.roll(activity, spot)
        drop, gold = r.get("drop"), int(r.get("gold") or 0)
        self.store.bump(rolls=1)
        self.store.spot_stat(key, drop and drop.get("rarity"), gold)
        if self.account is not None:
            lim = self.account.setdefault("limits", {})
            if "dropsLeftToday" in lim:
                lim["dropsLeftToday"] = max(0, lim["dropsLeftToday"] - 1)
            if drop:
                self.account.setdefault("items", []).append(drop)
            if gold:
                self.account["gold"] = self.account.get("gold", 0) + gold
        if gold:
            self.store.bump(gold=gold)
            self.event(f"💰 +{gold} gold bank dari {SPOTS[key][2]}")
        if drop:
            rar = drop.get("rarity", "common")
            self.store.bump(drops=1, rare=1 if rarity_rank(rar) >= 2 else 0)
            self.event(f"{RARITY_ICON.get(rar, '⚪')} {drop.get('name')} ({rar}) dari {SPOTS[key][2]}")
            if rarity_rank(rar) >= 2 and self.store.get("notify_rare"):
                await self.say(f"✨ <b>Drop {rar.upper()}!</b>\n{RARITY_ICON.get(rar)} "
                               f"<b>{drop.get('name')}</b> ({drop.get('kind')})\n📍 {SPOTS[key][2]}")
        self.store.save()
        return r

    async def _farm_loop(self):
        await asyncio.sleep(3)
        while True:
            try:
                if not self.store.get("farm_on"):
                    self.status = "paused"
                    await asyncio.sleep(5)
                    continue
                if self.account is None:
                    await self.refresh(full=True)
                if self.limits.get("dropsLeftToday", 1) <= 0:
                    self.status = "limit"
                    await asyncio.sleep(600)
                    await self.refresh()
                    continue
                self.status = "farming"
                await self.roll_once()
                self.last_error = ""
                delay = float(self.store.get("delay_s")) + random.uniform(0.3, 2.5)
            except ApiError as e:
                if e.code == "rate_limited":
                    delay = (e.retry_ms or 6000) / 1000 + random.uniform(0.5, 1.5)
                elif e.code in ("insufficient", "forbidden") or "limit" in e.msg.lower():
                    self.status = "limit"
                    self.event(f"⛔ Roll berhenti: {e.msg}")
                    delay = 900
                else:
                    self.last_error = e.msg
                    log.warning("roll error %s %s", e.code, e.msg)
                    delay = 30
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.exception("farm loop")
                delay = 30
            self.next_roll_at = time.time() + delay
            await asyncio.sleep(delay)

    # ------------------------------------------------------------------ bank
    async def _via_game(self, kind, amount):
        """While the game owns the save, the bot in the game does it on the Wallet page
        (the game's own Withdraw / Deposit Gold buttons)."""
        brain = getattr(self.game, "brain", None) if self.game else None
        if not brain or not self.game.running:
            return 0, "Game belum siap. Coba lagi sebentar."
        fut = asyncio.get_running_loop().create_future()
        brain.requests.append((kind, int(amount), fut))
        try:
            moved, msg = await asyncio.wait_for(asyncio.shield(fut), 240)
        except asyncio.TimeoutError:
            return 0, "⏳ Game sedang sibuk (duel/dialog). Permintaan tetap antre dan dikerjakan sebentar lagi."
        if moved:
            await self.refresh()
            return moved, f"✅ {msg}"
        return 0, f"❌ {msg}"

    async def deposit_pouch(self, amount=None):
        """Move pouch gold (save.gems) into the bank, the same way the game does.

        The save is lowered first and only then the deposit is sent, so a failed
        step can never leave the gold counted twice."""
        if self.game_owns_save():
            if amount is None:
                return 0, "Bot sedang main game: deposit otomatis dilakukan dari menu Wallet di dalam game."
            return await self._via_game("deposit", amount)
        async with self._save_lock:
            s = await self.api.get_save()
            save, rev = s.get("save"), s.get("rev")
            if not save:
                return 0, "Belum ada save game. Main dulu sekali di browser."
            gems = int(save.get("gems", 0))
            keep = int(self.store.get("keep_pouch")) if amount is None else 0
            left = int((await self.api.me())["limits"].get("depositLeftToday", 0))
            n = min(gems - keep, left, 1000, amount if amount else 10**9)
            if n < 1:
                return 0, "Tidak ada gold kantong yang bisa di-deposit sekarang."
            save["gems"] = gems - n
            rev = (await self.api.put_save(rev, save))["rev"]
            self._own_write(rev)
            try:
                d = await self.api.deposit(n)
            except ApiError as e:
                save["gems"] = gems
                self._own_write((await self.api.put_save(rev, save))["rev"])
                return 0, f"Deposit gagal: {e.msg}"
            credited = int(d.get("credited", n))
            if credited < n:  # server accepted less: give the rest back to the pouch
                save["gems"] = gems - credited
                self._own_write((await self.api.put_save(rev, save))["rev"])
        self.store.bump(deposited=credited)
        self.event(f"🏦 Deposit {credited} gold kantong → bank")
        self.store.save()
        await self.refresh()
        return credited, f"✅ {credited} gold masuk bank."

    async def withdraw_bank(self, amount):
        if self.game_owns_save():
            return await self._via_game("withdraw", amount)
        async with self._save_lock:
            s = await self.api.get_save()
            save, rev = s.get("save"), s.get("rev")
            if not save:
                return 0, "Belum ada save game."
            room = MAX_GEMS - int(save.get("gems", 0))
            n = min(int(amount), room, int((self.account or {}).get("gold", 0)))
            if n < 1:
                return 0, "Tidak ada gold bank untuk ditarik."
            await self.api.withdraw(n)
            save["gems"] = int(save.get("gems", 0)) + n
            try:
                self._own_write((await self.api.put_save(rev, save))["rev"])
            except ApiError as e:
                log.error("withdraw save failed after server withdraw: %s", e.msg)
                return n, f"⚠️ Gold ditarik tapi save gagal disimpan: {e.msg}"
        self.event(f"👛 Tarik {n} gold bank → kantong")
        await self.refresh()
        return n, f"✅ {n} gold kembali ke kantong."

    # ------------------------------------------------------------------ invites
    def invite_link(self):
        code = (self.invites or {}).get("code")
        return f"{SITE}/?ref={code}" if code else None

    async def load_invites(self):
        if not self.api.invite_key:
            self.invites = None
            return None
        try:
            self.invites = await self.api.invites_me()
        except ApiError as e:
            if e.code in ("not_found", "unauthorized"):
                body_ref = self.ref_code
                self.invites = await self.api.request(
                    "POST", "/api/invites/register", {"ref": body_ref} if body_ref else {},
                    invite=True)
            else:
                raise
        await self._try_referral()
        return self.invites

    async def _try_referral(self):
        """Attach the configured referral once, if this account has none yet."""
        inv = self.invites or {}
        ref = self.ref_code
        if not ref or ref == inv.get("code") or inv.get("referred") or inv.get("refState") not in (None, "none"):
            return
        if self.store.data.get("ref_tried") == ref:
            return
        self.store.data["ref_tried"] = ref
        self.store.save()
        try:
            r = await self.api.request("POST", "/api/invites/register", {"ref": ref}, invite=True)
            self.invites = r
            if r.get("referred"):
                self.event(f"🤝 Referral {ref} terpasang")
            else:
                self.event(f"🤝 Referral {ref} tidak diterima server: {r.get('why') or r.get('refState')}")
        except ApiError as e:
            self.event(f"🤝 Referral gagal: {e.msg}")

    async def claim_invite(self, task):
        """Claim a ready invite task and put the reward into the save, like the game."""
        inv = self.invites or {}
        t = next((x for x in inv.get("tasks", []) if x["id"] == task), None)
        if not t:
            return False, "Task tidak ditemukan."
        if self.game_owns_save():
            return False, "Bot sedang main game: hadiah diklaim dari menu Invite di dalam game."
        async with self._save_lock:
            s = await self.api.get_save()
            save, rev = s.get("save"), s.get("rev")
            if not save:
                return False, "Belum ada save game."
            if not self._bag_room(save, t["reward"].get("items", [])):
                return False, "Tas di game penuh, kosongkan dulu."
            r = await self.api.invites_claim(task)
            grant = r.get("grant") or t["reward"]
            gold = int(grant.get("gold", 0))
            save["gems"] = min(MAX_GEMS, int(save.get("gems", 0)) + gold)
            for it in grant.get("items", []):
                self._bag_add(save, it["id"], int(it.get("n", 1)))
            save.setdefault("flags", {})[f"inv:done:{task}"] = True
            self._own_write((await self.api.put_save(rev, save))["rev"])
            try:
                self.invites = await self.api.invites_ack(task)
            except ApiError:
                self.invites = r.get("status") or self.invites
        names = ", ".join(f"{i['id']}×{i.get('n', 1)}" for i in grant.get("items", []))
        msg = f"🎁 {INVITE_TASKS.get(task, task)}: +{gold} gold" + (f", {names}" if names else "")
        self.event(msg)
        self.store.save()
        return True, msg

    @staticmethod
    def _bag_room(save, items):
        inv = save.get("inv", [])
        free = sum(1 for x in inv if x is None)
        need = sum(1 for it in items if not any(x and x.get("id") == it["id"] for x in inv))
        return free >= need

    @staticmethod
    def _bag_add(save, item_id, n):
        inv = save.setdefault("inv", [])
        for x in inv:
            if x and x.get("id") == item_id:
                x["n"] = x.get("n", 0) + n
                return
        for i, x in enumerate(inv):
            if x is None:
                inv[i] = {"id": item_id, "n": n}
                return

    async def mark_shared(self):
        self.invites = await self.api.invites_share("copy")
        return self.invites

    # ------------------------------------------------------------------ market
    async def market_price(self, key):
        """Cheapest listing price (POKECRAFT) of an item key by other sellers."""
        rows = await self.api.all_listings("all", "price")
        prices = [float(x["priceUi"]) for x in rows
                  if x.get("kind") == "item" and not x.get("mine")
                  and (x.get("item") or {}).get("key") == key]
        return (min(prices) if prices else None), len(prices)

    async def gold_price(self):
        """Cheapest POKECRAFT per 100 gold among other sellers."""
        rows = await self.api.all_listings("all", "price")
        p = [float(x["priceUi"]) / x["gold"] * 100 for x in rows
             if x.get("kind") == "gold" and not x.get("mine") and x.get("gold")]
        return min(p) if p else None

    def suggest_price(self, low):
        mn = float(self.store.get("sell_min_price"))
        if low is None:
            return None
        p = low * (1 - float(self.store.get("sell_undercut_pct")) / 100)
        return max(mn, round(p, 2))

    async def my_listings(self):
        rows = await self.api.all_listings("all", "new")
        return [x for x in rows if x.get("mine")]

    async def _market_note(self, ok, msg=""):
        was = self.market_ok
        self.market_ok, self.market_msg, self.market_checked = ok, msg, time.time()
        if was is False and ok:
            self.event("🟢 Market buka lagi")
            await self.say("🟢 <b>Market Pokecraft sudah buka lagi!</b>\nItem & gold bisa dijual sekarang. Buka 🛒 Market.")
        elif was is not False and ok is False:
            self.event(f"🔴 Market ditutup server: {msg}")
            await self.say(f"🔴 <b>Market ditutup oleh server game</b>\n<i>{msg}</i>\n"
                           "Item & gold tetap aman di akun. Bot cek ulang tiap 30 menit dan kabari kalau sudah buka.")

    async def _listing_call(self, coro):
        try:
            r = await coro
        except ApiError as e:
            if e.code == "market_disabled":
                await self._market_note(False, e.msg)
            raise
        await self._market_note(True)
        return r

    async def probe_market(self):
        """Market state can only be learned by trying to list. List one common item at a
        price nobody pays and cancel it right away."""
        cand = [i for i in self.items if not i.get("locked") and i.get("rarity") == "common"]
        if not cand:
            return self.market_ok
        try:
            r = await self._listing_call(self.api.list_item(cand[0]["id"], "999999"))
        except ApiError as e:
            if e.code != "market_disabled":
                log.warning("market probe: %s %s", e.code, e.msg)
            return self.market_ok
        lid = (r.get("listing") or {}).get("id") or r.get("id") or r.get("listingId")
        if not lid:
            lid = next((x["id"] for x in await self.my_listings()
                        if (x.get("item") or {}).get("id") == cand[0]["id"]), None)
        if lid:
            await self.api.cancel_listing(lid)
        return True

    async def sell_item(self, item_id, price):
        r = await self._listing_call(self.api.list_item(item_id, fmt_price(price)))
        it = next((x for x in self.items if x["id"] == item_id), {})
        self.store.bump(listed=1)
        self.event(f"🛒 List {it.get('name', item_id)} @ {fmt_price(price)} POKECRAFT")
        self.store.save()
        await self.refresh()
        return r

    async def sell_gold(self, gold, price):
        r = await self._listing_call(self.api.list_gold(int(gold), fmt_price(price)))
        self.event(f"🛒 List {gold} gold @ {fmt_price(price)} POKECRAFT")
        self.store.save()
        await self.refresh()
        return r

    async def _auto_sell(self):
        if not self.store.get("auto_sell") or self.market_ok is False:
            return
        keep_rare = self.store.get("sell_keep_rare")
        cache = {}
        rows = await self.api.all_listings("all", "price")
        for it in list(self.items):
            if it.get("locked"):
                continue
            if keep_rare and rarity_rank(it.get("rarity")) >= 2:
                continue
            k = it.get("key")
            if k not in cache:
                ps = [float(x["priceUi"]) for x in rows if x.get("kind") == "item"
                      and not x.get("mine") and (x.get("item") or {}).get("key") == k]
                cache[k] = min(ps) if ps else None
            price = self.suggest_price(cache[k])
            if price is None:
                continue  # no reference price: leave it for a manual decision
            try:
                await self.sell_item(it["id"], price)
                await asyncio.sleep(2)
            except ApiError as e:
                self.event(f"🛒 Gagal list {it.get('name')}: {e.msg}")
                if e.code in ("rate_limited", "forbidden", "market_disabled"):
                    break

    # ------------------------------------------------------------------ maintenance
    async def _maint_loop(self):
        await asyncio.sleep(1)
        n = 0
        while True:
            try:
                await self.refresh(full=(n % 6 == 0))
                idle = self.player_idle()
                if not idle:
                    log.info("player active in browser: auto save edits wait")
                if self.store.get("auto_deposit") and idle:
                    gems = int(self.save_info.get("gems") or 0)
                    if gems - int(self.store.get("keep_pouch")) >= 1 and self.limits.get("depositLeftToday", 0) > 0:
                        _, msg = await self.deposit_pouch()
                        log.info("auto deposit: %s", msg)
                await self.load_invites()
                if self.store.get("auto_claim_invites") and self.invites and idle:
                    for t in self.invites.get("tasks", []):
                        if t.get("state") == "ready":
                            ok, msg = await self.claim_invite(t["id"])
                            if ok:
                                await self.say(msg)
                if self.market_ok is not True and time.time() - self.market_checked >= 1800:
                    await self.probe_market()
                await self._auto_sell()
                self.status = self.status if self.status != "starting" else "farming"
            except asyncio.CancelledError:
                raise
            except ApiError as e:
                self.last_error = e.msg
                log.warning("maint error %s %s", e.code, e.msg)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
                log.exception("maint loop")
            n += 1
            await asyncio.sleep(300)


def fmt_price(p):
    s = f"{float(p):.6f}".rstrip("0").rstrip(".")
    return s or "0"
