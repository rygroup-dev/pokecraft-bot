"""Decides what the character does next, from what the game shows (journey, quests, party).

Only objectives whose mechanics were checked in the game client are automated; anything
else is reported as skipped instead of being guessed at."""
import asyncio
import logging
import math
import os
import random
import time

from .battle import Battle
from .player import Paused

log = logging.getLogger("brain")

FOODS = ("cookie", "berry", "apple", "mega_berry", "honey_cake", "fruit_pie", "bread")


class Fails(dict):
    """Failure counters that forget a key 15 minutes after its last failure, so a task that
    failed for a temporary reason (NPC away, path blocked) is tried again later."""

    def __init__(self, ttl=900):
        super().__init__()
        self.ttl = ttl
        self.when = {}

    def get(self, key, default=0):
        if key in self and time.time() - self.when.get(key, 0) > self.ttl:
            del self[key]
        return super().get(key, default)

    def __setitem__(self, key, value):
        self.when[key] = time.time()
        super().__setitem__(key, value)


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
        self._fail = Fails()
        self._unreach = {}          # wild creature id -> time it was found unreachable
        self._bug_places = set()
        self._water_at = 0.0
        self._bag_at = 0.0
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
        if s.get("photo"):
            self.act("leave photo mode")     # photo mode hides the whole HUD
            await self.p.b.tap("Escape")
            await asyncio.sleep(0.8)
            return
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
        if s.get("swim") and not await self._water_task(s):
            await self.back_to_land(s)
            return
        if await self.heal_if_needed(s):
            return
        if await self.deposit_ui(s):
            return
        if await self.manage_bag(s):
            return
        if await self.turn_in(s):
            return
        if await self.take_offers(s):
            return
        if await self.notice_board(s):
            return
        if await self.quests(s):
            return
        if await self.journey(s):
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
        await self.p.talk(healer["id"], goals=["heal"], want=["heal"])
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
            log.info("deposit: Wallet tab not found (menus=%s)", [b["fid"] for b in await self.p.buttons()][:12])
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
            await self.say(f"ðŸ¦ Deposit {moved} gold kantong â†’ bank (dari dalam game)")
        else:
            log.info("deposit via wallet did not move gold (ok=%s)", ok)
        return True

    async def land_point(self, s, rmin=12, rmax=24, center=None, tries=10):
        """A random spot we can walk to on land (no swimming on the way), checked with A*."""
        cx, cz = center if center else (s["pos"]["x"], s["pos"]["z"])
        swimming = bool(s.get("swim"))
        for _ in range(tries):
            ang = random.uniform(0, 2 * math.pi)
            r = random.uniform(rmin, rmax)
            x, z = cx + r * math.cos(ang), cz + r * math.sin(ang)
            plan = await self.p.js(f"__pkbot.path({x:.1f}, {z:.1f}, {{near: 1.5, max: 30000}})")
            pts = plan.get("pts") or []
            if not plan.get("ok") or not pts or pts[-1][3]:
                continue                       # the spot itself must be dry land
            if swimming or not any(pt[3] for pt in pts):
                return x, z
        return None

    async def _water_task(self, s):
        """True when the current goal is across the water (crossing to Birchwick etc.)."""
        return any(q["id"] in ("jr_open", "jr_bank", "jr_cave") and q["phase"] == "active" for q in s["quests"])

    async def back_to_land(self, s):
        """In the water with nothing to do there: swim to the nearest dry land (or the village)."""
        p = s["pos"]
        if math.hypot(p["x"] - 46, p["z"] - 72) > 80:
            # far from the home island (quest givers live there): swim home instead of to the
            # nearest beach, so trips across the sea are not undone halfway
            self.act("in the water far from home: swimming back to the village")
            return await self.p.walk_to(46, 72, near=4, timeout=400)
        self.act("in the water: swimming back to land")
        for rmin, rmax in ((3, 12), (8, 25), (20, 45)):
            pt = await self.land_point(s, rmin, rmax, tries=12)
            if pt:
                return await self.p.walk_to(pt[0], pt[1], near=2, timeout=150)
        return await self.p.walk_to(46, 72, near=4, timeout=300)

    def _seed_for(self, s, crop=None):
        seeds = [x for x in s["inv"] if x["id"].endswith("_seed")]
        if crop:
            seeds = [x for x in seeds if x["id"] == f"{crop}_seed"]
        return max(seeds, key=lambda x: x["n"])["id"] if seeds else None

    async def buy_item(self, item, n):
        """Buy n of an item at Tilly's store (shop > Buy tab > row > Buy)."""
        if not await self.npc_out("tilly"):
            return False
        self.act(f"buy {n}x {item} at Tilly")
        await self.p.talk("tilly", goals=["openShop"])
        await asyncio.sleep(1.0)
        await self.p.click_button(fid="tab-buy", wait=0.6)
        if not await self.p.click_button(fid="row-" + item, wait=0.5):
            await self.p.close_menus()
            self.skipped[item] = "tidak dijual di toko Tilly"
            return False
        before = sum(x["n"] for x in (await self.p.st())["inv"] if x["id"] == item)
        for _ in range(n):
            if not await self.p.click_button(fid="act-go", wait=0.5):
                break
        await self.p.close_menus()
        after = sum(x["n"] for x in (await self.p.st())["inv"] if x["id"] == item)
        return after > before

    async def do_plant(self, s, n, crop=None):
        seed = self._seed_for(s, crop)
        if not seed and crop:
            # the crop's seed is not in the bag: buy it at Tilly's (she sells seeds)
            if await self.buy_item(f"{crop}_seed", max(n, 4)):
                s = await self.p.st()
                seed = next((x["id"] for x in s["inv"] if x["id"] == f"{crop}_seed"), None)
        if not seed:
            self.skipped["plant"] = f"tidak ada benih {crop or ''}".strip()
            return False
        empty = await self.p.js("__pkbot.farmland(4)")
        if len([c for c in empty if 0.6 <= c["d"] <= 3.2]) < min(n, 4):
            if not await self.till_somewhere(max(n, 4)):
                return False
        got = await self.p.plant(seed, n)
        self.act(f"planted {got} {seed}")
        if got:
            await self.p.water_crops()
        return got > 0

    async def do_harvest(self, s, n, crop=None):
        crops = await self.p.js("__pkbot.crops()")
        if crop:
            crops = [c for c in crops if c["crop"] == crop]
        ripe = [c for c in crops if c["stage"] >= 3]
        if ripe:
            got = await self.p.harvest_ripe()
            self.act(f"harvested {got}")
            return got > 0
        if not crops:
            return await self.do_plant(s, max(n, 4), crop)
        if time.time() - self._water_at > 120:
            near = [c for c in crops if c["d"] < 40]
            if near:
                self._water_at = time.time()
                await self.p.walk_to(near[0]["x"] + 0.5, near[0]["z"] + 1.5, near=1.5, timeout=60)
                await self.p.water_crops()
        self.act(f"crops growing ({len(crops)}), doing other things meanwhile")
        return "wait"

    async def place_specific(self, block, n):
        """Place n blocks of a given block id (quest: '8 planks for the seat', '6 lanterns').
        Uses a bag item that places that block; saws logs into planks when planks are short.
        Returns None when the bot has no way to get that block."""
        s = await self.p.st()
        item = None
        for x in s["inv"]:
            if await self.p.js(f"__pkbot.blockOf('{x['id']}')") == block:
                item = x
                break
        planks = await self.p.js("__pkx.F.Planks")
        if (not item or item["n"] < n) and block == planks:
            logs = sum(x["n"] for x in s["inv"] if x["id"] in ("oak_log", "birch_log"))
            if logs:
                self.act("saw logs into planks for building")
                await self.p.b.tap("KeyI")
                await asyncio.sleep(1)
                await self.p.click_button(fid="tab-craft", wait=0.8)
                for _ in range(min(3, logs)):
                    rec = "cr-planks_oak" if any(x["id"] == "oak_log" for x in (await self.p.st())["inv"]) else "cr-planks_birch"
                    await self.p.click_button(fid=rec, wait=0.4)
                    await self.p.click_button(fid="cr-go", wait=0.6)
                await self.p.close_menus()
                s = await self.p.st()
                item = next((x for x in s["inv"] if x["id"] == "blk_planks"), None)
        if not item:
            name = await self.p.js(f"__pkbot.blockName({block})")
            self.skipped[f"block {name}"] = "tidak punya blok ini (belum bisa dibuat)"
            return None
        if len(await self.p.js("__pkbot.placeSpots(3)")) < min(n, 3):
            spot = await self.p.js("__pkbot.findTillSpot(60, 6)")
            if not spot or not await self.p.walk_to(spot["x"], spot["z"], near=1.2, timeout=120):
                return False
        got = await self.p.place_blocks(item["id"], min(n, item["n"]))
        self.act(f"placed {got} {item['id']}")
        return got > 0

    async def donate_museum(self, n):
        """Give museum pieces (fish, bugs, shells, fossils...) to Ferris; catch some first if
        the bag has none (fish and bugs are museum pieces)."""
        s = await self.p.st()
        pieces = []
        for x in s["inv"]:
            d = await self.p.js(f"__pkbot.itemDef('{x['id']}')") or {}
            if d.get("museum"):
                pieces.append(x["id"])
        if not pieces:
            self.act("no museum pieces in the bag: catching fish first")
            return await self.go_fish(3) > 0
        if not await self.npc_out("ferris"):
            return False
        self.act(f"donate to the museum ({len(pieces)} piece kinds)")
        await self.p.talk("ferris", goals=["openMuseum"])
        await asyncio.sleep(1.0)
        done = 0
        for b in await self.p.buttons():
            if b["fid"].startswith("don-") and done < n:
                await self.p.b.click(b["x"], b["y"])
                await asyncio.sleep(0.8)
                done += 1
        await self.p.close_menus()
        if done:
            await self.say(f"🏛 Donasi {done} barang ke museum")
        return done > 0

    async def sell_for_quest(self, n):
        """Sell n items at Tilly (quest 'sell': every item sold counts). Sells bag items no quest,
        museum or befriending needs; picks flowers first if there is nothing to sell."""
        s = await self.p.st()
        keep = set(self.KEEP_ALWAYS) | {o.get("item") for q in s["quests"] for o in q["objs"] if o.get("item")}
        cands = []
        for x in s["inv"]:
            if x["id"] in keep or x["id"].endswith("_seed"):
                continue
            d = await self.p.js(f"__pkbot.itemDef('{x['id']}')") or {}
            if d.get("sell") is None or d.get("museum") or d.get("treat") or d.get("heal") or d.get("kind") in ("food", "tool", "seed"):
                continue
            cands.append(x)
        if sum(x["n"] for x in cands) < n:
            # gather something sellable (flowers lie everywhere)
            for it in ("flower_purple", "flower_orange", "flower_white", "flower_blue", "flower_pink"):
                if await self.p.js(f"__pkbot.decorFind('{it}', 80).length"):
                    await self.p.gather(it, n)
                    break
            return True
        shop = await self.nearest_shop()
        if not shop:
            return False
        self.act(f"sell {n} item(s) at {shop['name']} for a quest")
        await self.p.talk(shop["id"], goals=["openShop"])
        await asyncio.sleep(1.0)
        await self.p.click_button(fid="tab-sell", wait=0.8)
        sold = 0
        for x in cands:
            if sold >= n:
                break
            if await self.p.click_button(fid="row-" + x["id"], wait=0.4):
                await self.p.click_button(fid="act-all", wait=0.6) or await self.p.click_button(fid="act-go", wait=0.6)
                sold += x["n"]
        await self.p.close_menus()
        return sold > 0

    async def beat_trainer(self, trainer):
        """Challenge the trainer (the NPC whose talk has battle:<trainer>) and fight the duel."""
        npc = await self.p.js(f"__pkbot.battleNpc('{trainer}')")
        if not npc:
            self.skipped[trainer] = "trainer tidak ditemukan"
            return False
        s = await self.p.st()
        if self.party_frac(s) < 0.95:
            return await self.heal_if_needed(s, force=True)
        if not await self.npc_out(npc["id"]):
            return False
        self.act(f"challenge {npc['name']} ({trainer})")
        await self.p.talk(npc["id"], goals=["battle:" + trainer])
        await asyncio.sleep(1.5)
        if (await self.p.st()).get("battle"):
            await self.duel_finish()
            return True
        return False

    async def go_fish(self, n, want_item=None):
        """Walk to the nearest shore with open water and fish n times (or until want_item)."""
        s = await self.p.st()
        if (s.get("invCap") or 30) - len(s["inv"]) < 2:
            await self.manage_bag(s, min_free=99)
        spot = await self.p.js("__pkbot.fishSpot(60)")
        if not spot:
            log.info("no fishing spot within 60 m")
            return 0
        self.act(f"go fishing at {spot['x']:.0f},{spot['z']:.0f}")
        if not await self.p.walk_to(spot["x"], spot["z"], near=0.9, timeout=150):
            return 0
        if not await self.p.select_item("fishing_rod"):
            return 0
        await self.p.aim_point(spot["wx"], spot["wy"], spot["wz"])
        got = 0
        for _ in range(n):
            r = await self.p.fish_once()
            if r is None:
                await self.p.aim_point(spot["wx"], spot["wy"], spot["wz"])
                r = await self.p.fish_once()
                if r is None:
                    break
            if r in ("fish", "junk"):
                got += 1
            if want_item and any(x["id"] == want_item for x in (await self.p.st())["inv"]):
                break
            if not want_item and got >= n:
                break
        self.act(f"fishing done: {got} catch(es)")
        return got

    async def till_somewhere(self, n):
        """Go to open ground outside the protected village and till n patches."""
        here = await self.p.js("__pkbot.tillable(4)")
        if len(here) < 4:
            spot = await self.p.js("__pkbot.findTillSpot(60, 8)")
            if not spot:
                log.info("no open tillable ground within 60 m")
                return False
            self.act(f"walk to open ground {spot['x']:.0f},{spot['z']:.0f} to till ({spot['n']} patches)")
            if not await self.p.walk_to(spot["x"], spot["z"], near=1.2, timeout=120):
                return False
        got = await self.p.till(n)
        self.act(f"tilled {got} patch(es)")
        return got > 0

    async def wander(self, why):
        """Walk to another reachable meadow spot so new wild creatures come into range."""
        s = await self.p.st()
        pt = await self.land_point(s, 15, 30) or await self.land_point(s, 10, 25, center=(46, 72))
        self.act(f"{why}: walking to a new spot")
        if pt:
            await self.p.walk_to(pt[0], pt[1], near=3, timeout=60)
        else:
            await asyncio.sleep(5)
        return True

    async def notice_board(self, s):
        """Daily jobs on the village notice board: hand in the ready ones and take new ones
        (E on the board > pick the job; the game accepts or hands it in)."""
        jobs = await self.p.js("__pkbot.boardInfo()")
        ready = [j for j in jobs if j["phase"] == "ready"]
        active = [q for q in s["quests"] if q["id"].startswith("daily_") and q["phase"] == "active"]
        new = [j for j in jobs if j["phase"] == "none" and self._fail.get("board:" + j["id"], 0) < 2]
        job = ready[0] if ready else (new[0] if new and len(active) < 3 else None)
        if not job:
            return False
        board = next(iter(await self.p.js("__pkbot.props('board')")), None)
        if not board:
            return False
        self.act(f"notice board: {'hand in' if job['phase'] == 'ready' else 'take'} '{job['title']}'")
        if not await self.p.approach_prop(board):
            self._fail["board:" + job["id"]] = self._fail.get("board:" + job["id"], 0) + 1
            return True
        await self.p.interact()
        await asyncio.sleep(1.0)
        st = await self.p.st()
        # only pick a number when the board's own list is open (not a talk with someone else)
        if st.get("dlg"):
            await self.p.run_dialogue(timeout=20)
        elif any("choose" in l or "modal" in l for l in st.get("layers", [])):
            jobs2 = await self.p.js("__pkbot.boardInfo()")
            idx = next((j["i"] for j in jobs2 if j["id"] == job["id"]), None)
            if idx is not None:
                await self.p.b.tap(f"Digit{idx + 1}")
                await asyncio.sleep(1.0)
        await self.p.close_menus()
        after = next((j for j in await self.p.js("__pkbot.boardInfo()") if j["id"] == job["id"]), None)
        if after and after["phase"] == job["phase"]:
            self._fail["board:" + job["id"]] = self._fail.get("board:" + job["id"], 0) + 1
        elif job["phase"] == "ready":
            self.stats["quests"] += 1
            await self.say(f"📌 Daily selesai: <b>{job['title']}</b>")
        else:
            await self.say(f"📌 Daily diambil: <b>{job['title']}</b>")
        return True

    async def take_offers(self, s, radius=200):
        """Accept quests the game offers from nearby NPCs (dialogue action giveQuest)."""
        offers = await self.p.js("__pkbot.offers()")
        inside = {n["id"] for n in await self.p.js("__pkbot.npcs()") if n.get("state") == "inside"}
        for o in offers:
            if o["d"] > radius or self._fail.get("offer:" + o["id"], 0) >= 2:
                continue
            if o["giver"] in inside:
                continue                       # giver is at home right now (game schedule)
            if (s.get("invCap") or 30) - len(s["inv"]) < 3:
                # givers often hand over an item when the quest starts; the game disables the
                # "accept" answer while the bag is full, so make room first
                self._bag_at = 0.0
                if await self.manage_bag(s, min_free=3):
                    return True
            self.act(f"take quest '{o['title']}' from {o['name']} ({o['d']:.0f} m)")
            await self.p.talk(o["giver"], goals=["giveQuest:" + o["id"]])
            await self.p.close_menus()
            s2 = await self.p.st()
            if any(q["id"] == o["id"] for q in s2["quests"]):
                await self.say(f"ðŸ“œ Quest baru: <b>{o['title']}</b> dari {o['name']}")
            else:
                self._fail["offer:" + o["id"]] = self._fail.get("offer:" + o["id"], 0) + 1
                log.info("offer %s not taken (dialogue did not give it)", o["id"])
            return True
        return False

    KEEP_ALWAYS = {"hoe", "wooden_hoe", "watering_can", "bug_net", "fishing_rod", "shovel", "wooden_shovel",
                   "wooden_pickaxe", "wooden_axe", "stone_pickaxe", "stone_axe", "iron_pickaxe", "iron_axe",
                   "oak_log", "birch_log", "blk_planks", "stick"}

    TOOL_RANK = {  # same job, better tool last: keep only the best one of each
        "hoe": ["hoe", "wooden_hoe", "stone_hoe", "iron_hoe"],
        "can": ["watering_can"],
        "shovel": ["shovel", "wooden_shovel", "stone_shovel", "iron_shovel"],
        "rod": ["fishing_rod"],
        "net": ["bug_net"],
        "pick": ["wooden_pickaxe", "stone_pickaxe", "iron_pickaxe"],
        "axe": ["wooden_axe", "stone_axe", "iron_axe"],
    }

    async def drop_junk(self, s, keep):
        """Drop what can't be sold and isn't needed: duplicate/obsolete tools and unsellable
        leftovers (bag menu > slot > Drop). Returns how many slots were freed."""
        drop = []
        for group in self.TOOL_RANK.values():
            owned = [x for x in s["inv"] if x["id"] in group]
            if len(owned) > 1:
                best = max(owned, key=lambda x: group.index(x["id"]))
                drop += [x for x in owned if x is not best]
        for x in s["inv"]:
            if x in drop or x["id"] in keep or x["id"].endswith("_seed"):
                continue
            if any(x["id"] in g for g in self.TOOL_RANK.values()):
                continue
            d = await self.p.js(f"__pkbot.itemDef('{x['id']}')") or {}
            if d.get("sell") is None and d.get("kind") == "resource" and x["id"].startswith("blk_"):
                drop.append(x)                 # stray terrain blocks picked up while clearing paths
        if not drop:
            return 0
        # Dropped items fly back (ground items are pulled to the player within 2.9 m), so park
        # them in the personal bank vault instead (banker > Vault: bag slot > "Move all to vault").
        banker = next((n for n in await self.p.js("__pkbot.npcs()")
                       if n["id"].startswith("banker:") and n.get("state") != "inside"), None)
        if not banker:
            return 0
        self.act(f"store {len(drop)} unneeded item(s) in the bank vault: {[x['id'] for x in drop]}")
        await self.p.talk(banker["id"], goals=["openBank"])
        await asyncio.sleep(1.0)
        if not any("storage" in l or "vault" in l or "box" in l for l in (await self.p.st()).get("layers", [])):
            log.info("vault did not open (layers %s)", (await self.p.st()).get("layers"))
            await self.p.close_menus()
            return 0
        freed = 0
        for x in drop:
            if await self.p.click_button(fid=f"bag-{x['i']}", wait=0.4) and \
               await self.p.click_button(fid="st-quick", wait=0.6):
                now = await self.p.st()
                if not any(y["i"] == x["i"] and y["id"] == x["id"] for y in now["inv"]):
                    freed += 1
        await self.p.close_menus()
        return freed

    async def manage_bag(self, s, min_free=4):
        """Bag almost full: drop junk, sell what nothing needs to Tilly, donate museum pieces."""
        free = (s.get("invCap") or 30) - len(s["inv"])
        if free >= min_free or time.time() - self._bag_at < 600:
            return False
        self._bag_at = time.time()
        keep = set(self.KEEP_ALWAYS)
        for q in s["quests"]:
            for o in q["objs"]:
                if o.get("item"):
                    keep.add(o["item"])
        if await self.drop_junk(s, keep):
            s = await self.p.st()
            if (s.get("invCap") or 30) - len(s["inv"]) >= min_free:
                return True
        sell = []
        museum = []
        for x in s["inv"]:
            if x["id"] in keep or x["id"].endswith("_seed"):
                continue
            d = await self.p.js(f"__pkbot.itemDef('{x['id']}')") or {}
            if d.get("museum"):
                museum.append(x["id"])
                continue
            crop = d.get("kind") == "crop"     # harvest is for selling unless a quest wants it
            if d.get("sell") is None or (not crop and (d.get("treat") or d.get("heal") or d.get("kind") in ("food", "tool", "seed"))):
                continue
            sell.append(x["id"])
        shop = await self.nearest_shop() if sell else None
        if not shop:
            if museum and await self.npc_out("ferris"):
                return await self.donate_museum(len(museum))
            log.info("bag full (%d free): nothing to sell or no shopkeeper/Ferris out", free)
            return False
        self.act(f"bag full ({free} free): selling {len(sell)} kinds at {shop['name']}")
        await self.p.talk(shop["id"], goals=["openShop"])
        await asyncio.sleep(1.0)
        st = await self.p.st()
        if not any("shop" in l for l in st.get("layers", [])):
            await self.p.close_menus()
            return True
        await self.p.click_button(fid="tab-sell", wait=0.8)
        sold = 0
        for it in sell:
            if not await self.p.click_button(fid="row-" + it, wait=0.4):
                continue                       # Tilly does not buy this kind
            if not await self.p.click_button(fid="act-all", wait=0.6):
                await self.p.click_button(fid="act-go", wait=0.6)
            sold += 1
        await self.p.close_menus()
        after = await self.p.st()
        await self.say(f"🛍 Tas penuh, jual {sold} jenis barang ke Tilly (gold {s.get('gems')} → {after.get('gems')})")
        return True

    async def nearest_shop(self):
        """Nearest shopkeeper who is out right now (any shop counts for selling quests)."""
        for n in await self.p.js("__pkbot.shopNpcs()"):
            if n.get("state") != "inside":
                return n
        return None

    async def npc_out(self, npc_id):
        """False when the NPC is in its home spot but not drawn: asleep / indoors (its schedule).
        Uses the game's own schedule state when the NPC is far away and not rendered."""
        npcs = await self.p.js("__pkbot.npcs()")
        n = next((x for x in npcs if x["id"] == npc_id), None)
        # game actor.state is "inside" while the NPC is at home by its daily schedule
        return bool(n) and n.get("state") != "inside"

    async def turn_in(self, s):
        for q in s["quests"]:
            if q["phase"] != "ready" or q["id"].startswith("jr_"):
                continue
            giver = q.get("turnInTo") if q.get("turnInTo") not in (None, "anywhere") else q.get("giver")
            if not giver or ":" in giver and giver.startswith("vlg:"):
                pass
            if not giver or giver in ("anywhere", "board"):     # board jobs: notice_board()
                continue
            key = "turnin:" + q["id"]
            self._current_key = key
            if self._fail.get(key, 0) >= 3:
                continue
            if not await self.npc_out(giver):
                continue                       # asleep / indoors right now: try later
            self.act(f"turn in '{q['title']}' to {giver}")
            tr = await self.p.talk(giver, goals=["turnIn:" + q["id"]])
            if tr is None:
                self._fail[key] = self._fail.get(key, 0) + 1
                continue
            await self.p.close_menus()
            s2 = await self.p.st()
            if not any(x["id"] == q["id"] for x in s2["quests"]):
                self.stats["quests"] += 1
                await self.say(f"ðŸ“œ Quest selesai: <b>{q['title']}</b> (gold {s2['gems']})")
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
        self._current_key = jq["id"]
        before = self._progress_mark(s)
        ok = await h(s, jq)
        after = self._progress_mark(await self.p.st())
        if ok is False or (before is not None and before[2:] == after[2:]):
            # handler failed, or claimed success but nothing changed in the game (xp, gold,
            # quests, party, bag): count it so the same step is not retried in a tight loop
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
        if not await self.p.js(f"__pkbot.discoverable('{place_id}')"):
            # game bug: the place is not in the game's discoverable place table, so standing in
            # it never counts as a visit. Do not walk back and forth for nothing.
            known = set(self.store.data.get("bug_places", [])) if self.store else set()
            if place_id not in self._bug_places and place_id not in known:
                self._bug_places.add(place_id)
                if self.store:
                    self.store.data["bug_places"] = sorted(known | {place_id})
                    self.store.save()
                await self.say(f"🐞 Bug game: <b>{place_id}</b> tidak bisa 'ditemukan' di versi game ini "
                               f"(tidak ada di daftar tempat game), quest kunjungan ke sana dilewati.")
            self.skipped[place_id] = "bug game: tempat tidak terdaftar, kunjungan tidak terhitung"
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
            return await self.wander("no weak wild creatures near")
        now = time.time()
        self._unreach = {k: t for k, t in self._unreach.items() if now - t < 600}
        w = None
        for c in sorted(cands, key=lambda c: (c["level"] + c["d"] / 25, c["d"])):
            if c["id"] in self._unreach:
                continue
            plan = await self.p.js(f"__pkbot.path({c['x']}, {c['z']}, {{near: 1.8, max: 40000}})")
            if plan.get("ok"):
                w = c
                break
            self._unreach[c["id"]] = now
            log.info("wild %s Lv%s at %.0f,%.0f is not reachable on foot", c["species"], c["level"], c["x"], c["z"])
        if not w:
            return await self.wander("no reachable weak wild creature")
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
            await self.say(f"ðŸ¾ Teman baru bergabung! ({res})")
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
                await self.say(f"ðŸ¾ {w['species']} jadi teman baru!")
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
                self._current_key = key
                if self._fail.get(key, 0) >= 3:
                    continue
                left = max(1, (o.get("n") or 1) - (o.get("have") or 0))
                kind = o["kind"]
                self.act(f"quest '{q['title']}': {kind} ({o.get('have') or 0}/{o.get('n') or 1})")
                if kind == "talk" and o.get("npc"):
                    ok = await self.p.talk(o["npc"]) is not None
                    await self.p.close_menus()
                elif kind == "visit" and o.get("place"):
                    ok = await self.go_place(o["place"])
                elif kind == "win":
                    ok = await self.duel_weakest(s)
                elif kind == "befriend":
                    ok = await self.befriend_one(s)
                elif kind == "jump":
                    for _ in range(min(left, 40)):
                        await self.p.b.tap("Space", 0.08)
                        await asyncio.sleep(0.75)
                    ok = True
                elif kind == "sprint":
                    # sprint only counts on land: run between walkable spots near the village
                    pt = await self.land_point(s, 15, 28) or await self.land_point(s, 10, 22, center=(46, 72))
                    ok = bool(pt) and await self.p.walk_to(pt[0], pt[1], near=2.5, timeout=60)
                elif kind == "emote":
                    ok = await self.p.emote(o.get("emote") or "wave")
                elif kind == "pet":
                    ok = await self.p.pet_buddy()
                elif kind in ("have", "gain") and o.get("item"):
                    have_now = sum(x["n"] for x in s["inv"] if x["id"] == o["item"])
                    need = (o.get("n") or 1) - have_now
                    if need <= 0:
                        ok = True          # already in the bag; the quest turns ready on its own
                    elif (await self.p.js(f"__pkbot.itemDef('{o['item']}')") or {}).get("kind") == "crop":
                        # a harvest item (wheat, pumpkin ...): grow it
                        res = await self.do_harvest(s, need, crop=o["item"])
                        if res == "wait":
                            continue
                        ok = res
                    elif o["item"] in ("old_boot", "pebble", "clay_pot"):
                        # fishing junk (game: 10% of bites are junk, 45% of those an old boot)
                        ok = await self.go_fish(15, want_item=o["item"]) > 0
                    elif await self.p.js(f"__pkbot.decorFind('{o['item']}', 120).length"):
                        ok = await self.p.gather(o["item"], need) > 0
                    else:
                        self.skipped[q["id"]] = f"item '{o['item']}' tidak bisa dipungut di sekitar"
                        self._fail[key] = 99      # look again in 15 minutes, not every loop
                        continue
                elif kind == "bug":
                    got = 0
                    for _ in range(min(left, 4)):
                        if await self.p.catch_bug():
                            got += 1
                    if not got and not await self.p.js("__pkbot.bugs(50).length"):
                        await self.wander("no bugs flying near")
                    ok = got > 0
                elif kind == "fish":
                    ok = await self.go_fish(min(left, 5)) > 0
                elif kind == "dig":
                    got = 0
                    for _ in range(min(left, 4)):
                        if await self.p.dig_once():
                            got += 1
                    ok = got > 0
                elif kind == "donate":
                    ok = await self.donate_museum(left)
                elif kind == "sell":
                    ok = await self.sell_for_quest(left)
                elif kind == "beat" and o.get("trainer"):
                    ok = await self.beat_trainer(o["trainer"])
                elif kind == "plant":
                    ok = await self.do_plant(s, left, o.get("crop"))
                elif kind == "harvest":
                    res = await self.do_harvest(s, left, o.get("crop"))
                    if res == "wait":
                        continue               # crops still growing: do something else meanwhile
                    ok = res
                elif kind == "place" and o.get("block"):
                    ok = await self.place_specific(o["block"], left)
                    if ok is None:
                        self._fail[key] = 99
                        continue
                elif kind == "place" and not o.get("block"):
                    # any placement counts (game emits "place" for tilled farmland too)
                    ok = await self.till_somewhere(min(left, 12))
                else:
                    self.skipped[q["id"]] = f"tugas '{kind}' belum didukung"
                    self._fail[key] = 99
                    continue
                s2 = await self.p.st()
                q2 = next((x for x in s2["quests"] if x["id"] == q["id"]), None)
                prog = q2["objs"][i].get("have") if q2 else None
                if q2 and prog == o.get("have"):
                    ok = False            # nothing changed in the game: count as a failed try
                    log.info("quest %s obj %d (%s) did not move", q["id"], i, kind)
                if not ok:
                    self._fail[key] = self._fail.get(key, 0) + 1
                return True
        return False

    async def grind(self, s):
        """Nothing else to do: duel weak wild creatures (XP + gold per win)."""
        self.status = "grind duels"
        await self.duel_weakest(s)

    def _progress_mark(self, s):
        if not s.get("pos"):
            return None
        q = tuple(sorted((x["id"], x.get("phase"), tuple(o.get("have") or 0 for o in x["objs"]))
                         for x in s.get("quests", [])))
        p = s["pos"]
        return (round(p["x"] / 3), round(p["z"] / 3), s.get("xp"), s.get("lvl"), s.get("gems"), q,
                len(s.get("party", [])), sum(x["n"] for x in s.get("inv", [])))

    async def self_heal(self):
        """Nothing changed in the game for minutes: reset input, get free, try another task."""
        log.warning("SELF-HEAL: no progress for %ds (last: %s)", int(time.time() - self._mark_at), self.last_action)
        await self.say(f"ðŸ›  Bot sempat diam, memperbaiki diri sendiriâ€¦\n<i>{self.last_action[:120]}</i>")
        for k in ("KeyW", "KeyA", "KeyS", "KeyD", "ShiftLeft", "KeyG"):
            await self.p.b.key(k, False)
        await self.p.close_menus()
        await self.p.unstick()
        key = self._current_key
        if key:
            self._fail[key] = 99          # skip this task for a while (Fails forgets it after 15 min)
        self._unreach.clear()

    async def run(self, stop):
        self._mark, self._mark_at, self._current_key = None, time.time(), None
        pause_flag = os.path.join(os.path.dirname(self.store.path), "pause.flag") if self.store and hasattr(self.store, "path") else None
        self.p.pause_file = pause_flag
        while not stop.is_set():
            if pause_flag and os.path.exists(pause_flag):
                # paused (maintenance / testing in the same game window): no actions at all
                self.status = "paused"
                await asyncio.sleep(2)
                self._mark_at = time.time()
                continue
            try:
                s = await self.p.st()
                m = self._progress_mark(s)
                if m != self._mark:
                    self._mark, self._mark_at = m, time.time()
                elif time.time() - self._mark_at > 240 and s.get("phase") == "play":
                    await self.self_heal()
                    self._mark_at = time.time()
                t0 = time.time()
                await asyncio.wait_for(self.step(), 300)   # watchdog: no single step may hang
                if time.time() - t0 < 0.4:
                    await asyncio.sleep(1.0)               # a step that did nothing: never spin
            except Paused:
                log.info("PAUSED (pause.flag)")
            except asyncio.TimeoutError:
                log.warning("WATCHDOG step took > 5 min (last: %s), resetting", self.last_action)
                await self.p.hold(["KeyW", "ShiftLeft"], False)
                await self.p.close_menus()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.exception("brain step: %s", e)
                await asyncio.sleep(3)
