"""Decides what the character does next, from what the game shows (journey, quests, party).

Only objectives whose mechanics were checked in the game client are automated; anything
else is reported as skipped instead of being guessed at."""
import asyncio
import logging
import math
import time

from .battle import Battle

log = logging.getLogger("brain")

FOODS = ("cookie", "berry", "apple", "mega_berry", "honey_cake", "fruit_pie", "bread")


class Brain:
    def __init__(self, player, notify=None, store=None):
        self.p = player
        self.bt = Battle(player)
        self.notify = notify
        self.store = store
        self._deposit_at = 0.0
        self.status = "starting"
        self.last_action = ""
        self.skipped = {}            # quest id -> reason
        self.stats = {"duels": 0, "wins": 0, "befriended": 0, "quests": 0, "gold_start": None}
        self._fail = {}
        self._heal_fail = 0
        self._heal_pause = 0.0

    async def say(self, text):
        log.info("MILESTONE %s", text)
        if self.notify:
            try:
                await self.notify(text)
            except Exception:  # noqa: BLE001
                pass

    def act(self, text):
        self.last_action = text
        log.info("ACT %s", text)

    # ------------------------------------------------------------------ main step
    async def step(self):
        s = await self.p.st()
        ph = s.get("phase")
        if ph == "title":
            self.act("title screen: continue")
            await self.p.b.tap("KeyC")
            await asyncio.sleep(4)
            return
        if ph != "play" or not s.get("pos"):
            await asyncio.sleep(2)
            return
        if self.stats["gold_start"] is None:
            self.stats["gold_start"] = s.get("gems")
        if s.get("battle"):
            self.act("battle in progress")
            await self.duel_finish()
            return
        if s.get("dlg"):
            await self.p.run_dialogue()
            return
        if s.get("menus") or s.get("busy"):
            await self.p.close_menus()
            return
        if await self.heal_if_needed(s):
            return
        if await self.deposit_ui(s):
            return
        if await self.turn_in(s):
            return
        if await self.journey(s):
            return
        if await self.quests(s):
            return
        await self.grind(s)

    # ------------------------------------------------------------------ helpers
    def party_frac(self, s):
        tot = sum((c.get("max") or 0) for c in s["party"])
        cur = sum(max(0, c["hp"]) for c in s["party"])
        return cur / tot if tot else 1.0

    async def heal_if_needed(self, s, force=False):
        if not s["party"]:
            return False
        lead = s["party"][0]
        lead_frac = lead["hp"] / lead["max"] if lead.get("max") else 1.0
        if not force and self.party_frac(s) >= 0.5 and lead_frac >= 0.4:
            return False
        npcs = await self.p.js("__pkbot.npcs()")
        healer = next((n for n in npcs if n["id"].startswith("healer:")), None)
        if not healer:
            return False
        if time.time() < self._heal_pause:
            return False
        self.act(f"heal party at {healer['name']} ({self.party_frac(s):.0%} HP)")
        await self.p.talk(healer["id"], want=["heal"])
        await self.p.close_menus()
        after = await self.p.st()
        if self.party_frac(after) < 0.999:
            self._heal_fail += 1
            log.warning("heal did not fill HP (%.0f%%), fail %d", self.party_frac(after) * 100, self._heal_fail)
            if self._heal_fail >= 2:
                self._heal_pause = time.time() + 300
                self._heal_fail = 0
        else:
            self._heal_fail = 0
        return True

    async def deposit_ui(self, s):
        """Move pouch gold to the bank with the game's own Wallet page (Bag > Wallet >
        Deposit Gold), keeping `keep_pouch` gold for seeds and healing."""
        if not self.store or not self.store.get("auto_deposit"):
            return False
        keep = int(self.store.get("keep_pouch") or 0)
        gems = int(s.get("gems") or 0)
        if gems - keep < 200 or time.time() - self._deposit_at < 600:
            return False
        self._deposit_at = time.time()
        self.act(f"deposit pouch gold via Wallet ({gems} in pouch, keep {keep})")
        await self.p.b.tap("KeyI")
        await asyncio.sleep(1)
        if not await self.p.click_button(fid="tab-wallet", wait=1.2):
            await self.p.close_menus()
            return False
        if not await self.p.click_button(fid="deposit", wait=0.8):
            log.info("deposit button not available (limit reached or wallet not ready)")
            await self.p.close_menus()
            return False
        await self.p.click_button(fid="all", wait=0.3)
        for _ in range(keep // 10):
            await self.p.b.tap("ArrowDown")
            await asyncio.sleep(0.03)
        ok = await self.p.click_button(fid="ok", wait=2.0)
        await self.p.close_menus()
        after = await self.p.st()
        moved = gems - int(after.get("gems") or 0)
        if ok and moved > 0:
            await self.say(f"🏦 Deposit {moved} gold kantong → bank (dari dalam game)")
        else:
            log.info("deposit via wallet did not move gold (ok=%s)", ok)
        return True

    async def turn_in(self, s):
        for q in s["quests"]:
            if q["phase"] != "ready" or q["id"].startswith("jr_"):
                continue
            giver = q.get("turnInTo") if q.get("turnInTo") not in (None, "anywhere") else q.get("giver")
            if not giver or ":" in giver and giver.startswith("vlg:"):
                pass
            if not giver or giver == "anywhere":
                continue
            key = "turnin:" + q["id"]
            if self._fail.get(key, 0) >= 3:
                continue
            self.act(f"turn in '{q['title']}' to {giver}")
            tr = await self.p.talk(giver, want=["here", "hand", "brought", "done", "finished", "give", "yes"])
            if tr is None:
                self._fail[key] = self._fail.get(key, 0) + 1
                continue
            await self.p.close_menus()
            s2 = await self.p.st()
            if not any(x["id"] == q["id"] for x in s2["quests"]):
                self.stats["quests"] += 1
                await self.say(f"📜 Quest selesai: <b>{q['title']}</b> (gold {s2['gems']})")
            else:
                self._fail[key] = self._fail.get(key, 0) + 1
            return True
        return False

    # ------------------------------------------------------------------ journey
    async def journey(self, s):
        jq = next((q for q in s["quests"] if q["id"].startswith("jr_") and q["phase"] == "active"), None)
        if not jq:
            return False
        h = getattr(self, "jr_" + jq["id"][3:], None)
        if not h:
            self.skipped[jq["id"]] = "belum didukung"
            return False
        if self._fail.get(jq["id"], 0) >= 5:
            self.skipped[jq["id"]] = "gagal 5x"
            return False
        self.status = f"journey: {jq['title']}"
        ok = await h(s, jq)
        if ok is False:
            self._fail[jq["id"]] = self._fail.get(jq["id"], 0) + 1
        return True

    async def jr_wood(self, s, q):
        trees = await self.p.js("__pkbot.trees(40)")
        if not trees:
            return False
        t = trees[0]
        self.act(f"chop tree at {t['x']},{t['z']}")
        if not await self.p.walk_to(t["x"] + 0.5, t["z"] + 0.5, near=2.2):
            return False
        return await self.p.break_block(t["x"], t["y"], t["z"])

    async def jr_craft(self, s, q):
        inv = {x["id"]: x["n"] for x in s["inv"]}
        if inv.get("blk_planks", 0) < 8 and inv.get("oak_log", 0) + inv.get("birch_log", 0) < 2:
            return await self.jr_wood(s, q)
        await self.p.b.tap("KeyI")
        await asyncio.sleep(1)
        await self.p.click_button(fid="tab-craft", wait=0.8)
        for _ in range(3):
            if (await self._count("blk_planks")) >= 8:
                break
            rec = "cr-planks_oak" if (await self._count("oak_log")) else "cr-planks_birch"
            await self.p.click_button(fid=rec, wait=0.4)
            await self.p.click_button(fid="cr-go", wait=0.6)
        if (await self._count("stick")) < 4:
            await self.p.click_button(fid="cr-sticks", wait=0.4)
            await self.p.click_button(fid="cr-go", wait=0.6)
        await self.p.close_menus()
        if not await self.use_station("workbench"):
            return False
        for rec in ("cr-wooden_pickaxe", "cr-wooden_axe"):
            await self.p.click_button(fid=rec, wait=0.4)
            await self.p.click_button(fid="cr-go", wait=0.7)
        await self.p.close_menus()
        return True

    async def _count(self, item):
        s = await self.p.st()
        return sum(x["n"] for x in s["inv"] if x["id"] == item)

    async def use_station(self, name):
        blocks = await self.p.js("__pkbot.findBlocks(/^%s$/i, 70)" % name)
        for w in blocks[:3]:
            for dx, dz in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                y = await self.p.js(f"__pkbot.stand({w['x'] + dx},{w['z'] + dz},{w['y']})")
                if y is None:
                    continue
                if not await self.p.walk_to(w["x"] + dx + 0.5, w["z"] + dz + 0.5, near=0.9, timeout=120):
                    continue
                if await self.p.aim(w["x"], w["y"], w["z"]):
                    await self.p.interact()
                    await asyncio.sleep(1)
                    if (await self.p.st()).get("menus"):
                        return True
        return False

    async def jr_hunt(self, s, q):
        return await self.duel_weakest(s)

    async def jr_level(self, s, q):
        return await self.duel_weakest(s)

    async def jr_heal(self, s, q):
        return await self.heal_if_needed(s, force=True)

    async def jr_shop(self, s, q):
        self.act("visit Tilly's store")
        tr = await self.p.talk("tilly")
        await asyncio.sleep(0.8)
        await self.p.close_menus()
        return tr is not None

    async def jr_friend(self, s, q):
        return await self.befriend_one(s)

    async def jr_open(self, s, q):
        return await self.go_place("vil_birchwick")

    async def jr_cave(self, s, q):
        return await self.go_place("cave_3")

    async def jr_bank(self, s, q):
        tr = await self.p.talk("vlg:vil_birchwick:bank")
        await self.p.close_menus()
        return tr is not None

    async def go_place(self, place_id):
        places = await self.p.js("__pkbot.places()")
        pl = next((x for x in places if x["id"] == place_id), None)
        if not pl:
            return False
        self.act(f"travel to {pl['name']}")
        return await self.p.walk_to(pl["x"], pl["z"], near=4, timeout=600)

    # ------------------------------------------------------------------ duels
    async def duel_weakest(self, s):
        lead = next((c for c in s["party"] if c["hp"] > 0), None)
        if not lead:
            return await self.heal_if_needed(s, force=True)
        wilds = await self.p.js("__pkbot.wilds(70)")
        cands = [w for w in wilds if w["level"] <= lead["level"] + 1]
        if not cands:
            # nothing weak nearby: wander to a random meadow point around the village
            self.act("no weak wild creatures near, walking around")
            p = s["pos"]
            ang = (time.time() * 0.7) % (2 * math.pi)
            await self.p.walk_to(p["x"] + 18 * math.cos(ang), p["z"] + 18 * math.sin(ang), near=3, timeout=40)
            return True
        w = min(cands, key=lambda c: (c["level"] + c["d"] / 25, c["d"]))
        self.act(f"go duel {w['species']} Lv{w['level']} ({w['d']:.0f} m)")
        await self.ensure_empty_hand_for_duel()
        if not await self.approach_wild(w["id"]):
            self.act(f"could not reach {w['species']}")
            return False
        self.act(f"duel {w['species']} Lv{w['level']}")
        await self.p.interact()
        await asyncio.sleep(1.5)
        if not (await self.p.st()).get("battle"):
            return False
        return await self.duel_finish()

    async def duel_finish(self, befriend=False):
        before = await self.p.st()
        res, lines = await self.bt.run(befriend=befriend)
        await asyncio.sleep(1.2)
        after = await self.p.st()
        self.stats["duels"] += 1
        won = any("tuckered out" in l or "fainted" in l and "Wild" in l for l in lines) or after.get("gems", 0) > before.get("gems", 0)
        if any("joined your party" in l or "went to the Box" in l for l in lines):
            self.stats["befriended"] += 1
            await self.say(f"🐾 Teman baru bergabung! ({res})")
        elif won:
            self.stats["wins"] += 1
        await asyncio.sleep(4.2)       # game duel cooldown is 4 s
        return True

    async def ensure_empty_hand_for_duel(self):
        # holding food turns "Duel" into "Give treat"; hold a tool instead
        s = await self.p.st()
        held = next((x["id"] for x in s["inv"] if x["i"] == s["sel"]), None)
        if held in FOODS:
            for tool in ("wooden_axe", "wooden_pickaxe", "hoe", "shovel"):
                if await self.p.select_item(tool):
                    return

    async def approach_wild(self, cid, timeout=60):
        t_end = time.time() + timeout
        while time.time() < t_end:
            cur = next((x for x in await self.p.js("__pkbot.wilds(80)") if x["id"] == cid), None)
            if not cur:
                return False
            s = await self.p.st()
            if s.get("battle"):
                return True
            f = s.get("focus") or {}
            if f.get("k") == "wild" and f.get("cid") == cid:
                return True
            if cur["d"] > 1.8:
                await self.p.walk_to(cur["x"], cur["z"], near=1.6, timeout=25)
            else:
                await self.p.face_xz(cur["x"], cur["z"])
                await asyncio.sleep(0.2)
        return False

    # ------------------------------------------------------------------ befriending
    async def befriend_one(self, s):
        """Give treats to a wild creature until it trusts you (game: +2 favourite, +1 treat>=15,
        +0.5 other food; friends at 3)."""
        hotbar = {x["id"]: x for x in s["inv"] if x["i"] < 9}
        wilds = await self.p.js("__pkbot.wilds(70)")
        best = None
        for w in wilds:
            fav = await self.p.js(f"__pkbot.favourite('{w['species']}')")
            for item in ([fav] if fav else []) + list(FOODS):
                if item in hotbar:
                    tv = await self.p.js(f"__pkbot.treatValue('{item}')") or {}
                    gain = 2 if item == fav else 1 if (tv.get("treat") or 0) >= 15 else 0.5
                    need = math.ceil(3 / gain)
                    if hotbar[item]["n"] >= need:
                        cand = (need, w["d"], w, item)
                        if best is None or cand[:2] < best[:2]:
                            best = cand
                        break
        if not best:
            self.skipped["jr_friend"] = "makanan di hotbar kurang"
            return False
        need, _, w, item = best
        self.act(f"befriend {w['species']} with {need}x {item}")
        await self.p.select_item(item)
        for _ in range(need + 2):
            if not await self.approach_wild(w["id"], timeout=40):
                break
            await self.p.interact()
            await asyncio.sleep(0.9)
            s2 = await self.p.st()
            if s2.get("menus"):            # "new friend" card
                self.stats["befriended"] += 1
                await self.say(f"🐾 {w['species']} jadi teman baru!")
                await self.p.close_menus()
                return True
        return False

    # ------------------------------------------------------------------ other quests
    async def quests(self, s):
        for q in s["quests"]:
            if q["id"].startswith("jr_") or q["phase"] != "active":
                continue
            for i, o in enumerate(q["objs"]):
                done = q["lines"][i]["done"] if i < len(q["lines"]) else False
                if done:
                    continue
                key = f"{q['id']}:{i}"
                if self._fail.get(key, 0) >= 3:
                    continue
                if o["kind"] == "talk" and o.get("npc"):
                    self.act(f"quest '{q['title']}': talk to {o['npc']}")
                    ok = await self.p.talk(o["npc"]) is not None
                    await self.p.close_menus()
                elif o["kind"] == "visit" and o.get("place"):
                    ok = await self.go_place(o["place"])
                elif o["kind"] in ("win", "beat") and o["kind"] == "win":
                    ok = await self.duel_weakest(s)
                else:
                    self.skipped[q["id"]] = f"tugas '{o['kind']}' belum didukung"
                    continue
                if not ok:
                    self._fail[key] = self._fail.get(key, 0) + 1
                return True
        return False

    async def grind(self, s):
        """Nothing else to do: duel weak wild creatures (XP + gold per win)."""
        self.status = "grind duels"
        await self.duel_weakest(s)

    async def run(self, stop):
        while not stop.is_set():
            try:
                await asyncio.wait_for(self.step(), 300)   # watchdog: no single step may hang
            except asyncio.TimeoutError:
                log.warning("WATCHDOG step took > 5 min (last: %s), resetting", self.last_action)
                await self.p.hold(["KeyW", "ShiftLeft"], False)
                await self.p.close_menus()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("brain step: %s", e)
                await asyncio.sleep(3)
