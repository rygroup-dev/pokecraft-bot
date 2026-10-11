"""Low-level character control on top of Browser + agent.js: look, walk, aim, use, talk.

Every action is real input (keys, mouse drags, clicks). State is read back from the game
after each step, so the bot reacts to what actually happened instead of assuming."""
import asyncio
import json
import logging
import math
import os
import time

log = logging.getLogger("player")

AGENT_JS = os.path.join(os.path.dirname(__file__), "agent.js")
LOOK_RAD_PER_PX = 0.0024      # controls: lookX = movementX * 0.0024 * sensitivity (sens 1)


def wrap(a):
    return (a + math.pi) % (2 * math.pi) - math.pi


class Stuck(Exception):
    pass


class Player:
    def __init__(self, browser):
        self.b = browser
        self.pitch_sign = None     # learned once: how pitch reacts to a vertical drag

    # ------------------------------------------------------------------ plumbing
    async def inject(self):
        with open(AGENT_JS, encoding="utf-8") as f:
            return await self.b.eval(f.read())

    async def js(self, expr, timeout=30):
        v = await self.b.eval(f"JSON.stringify({expr})", timeout)
        return json.loads(v) if v is not None else None

    async def st(self):
        s = await self.js("window.__pkbot ? __pkbot.state() : {phase:'noagent'}")
        if s.get("phase") == "noagent":
            await self.inject()
            s = await self.js("__pkbot.state()")
        return s

    # ------------------------------------------------------------------ looking
    async def turn(self, dyaw, dpitch=0.0):
        """Turn camera by radians (yaw left/right, pitch) using mouse drags."""
        dx = -dyaw / LOOK_RAD_PER_PX
        dy = 0.0
        if dpitch:
            if self.pitch_sign is None:
                self.pitch_sign = 1.0   # measured: drag dy +60 -> pitch +0.144
            dy = self.pitch_sign * dpitch / LOOK_RAD_PER_PX
        # tiny drags count as clicks in drag mode (< 4 px), so skip them
        if abs(dx) < 5:
            dx = 0
        if abs(dy) < 5:
            dy = 0
        while abs(dx) > 0 or abs(dy) > 0:
            sx = max(-600, min(600, dx))
            sy = max(-300, min(300, dy))
            await self.b.drag_look(sx, sy)
            dx -= sx
            dy -= sy
            if abs(dx) < 5:
                dx = 0
            if abs(dy) < 5:
                dy = 0

    async def face_xz(self, x, z, tol=0.12):
        s = await self.st()
        p = s["pos"]
        want = math.atan2(-(x - p["x"]), -(z - p["z"]))
        d = wrap(want - s["yaw"])
        if abs(d) > tol:
            await self.turn(d)
        return d

    async def aim(self, x, y, z, tries=6):
        """Point the camera ray at block (x,y,z) and confirm the game reports it as hit."""
        tx, ty, tz = x + 0.5, y + 0.5, z + 0.5
        for _ in range(tries):
            r = await self.js("__pkbot.ray()")
            ox, oy, oz = r["o"]
            dx, dy, dz = r["d"]
            vx, vy, vz = tx - ox, ty - oy, tz - oz
            n = math.sqrt(vx * vx + vy * vy + vz * vz) or 1
            vx, vy, vz = vx / n, vy / n, vz / n
            a_cur, a_want = math.atan2(-dx, -dz), math.atan2(-vx, -vz)
            e_cur, e_want = math.asin(max(-1, min(1, dy))), math.asin(max(-1, min(1, vy)))
            dyaw, delev = wrap(a_want - a_cur), e_want - e_cur
            hit = await self.js("__pkbot.hit()")
            if hit and (hit["x"], hit["y"], hit["z"]) == (x, y, z):
                return True
            # looking up lowers pitch in this camera (pitch > 0 looks down)
            await self.turn(dyaw, -delev)
            await asyncio.sleep(0.12)
        hit = await self.js("__pkbot.hit()")
        return bool(hit and (hit["x"], hit["y"], hit["z"]) == (x, y, z))

    # ------------------------------------------------------------------ walking
    async def hold(self, codes, down):
        for c in codes:
            await self.b.key(c, down)

    async def walk_to(self, x, z, near=1.6, timeout=180, sprint=True):
        """Walk (and swim) to (x,z) along an A* path. Returns True when within `near`.
        When stuck: jump, back off and sidestep, break a natural block in the way (only if the
        game allows breaking it there), else mark the spot as blocked and plan around it."""
        t_end = time.time() + timeout
        avoid = set()
        replans = 0
        s = await self.st()
        if s.get("pos"):
            log.info("WALK to %.1f,%.1f from %.1f,%.1f (%.0f m)", x, z, s["pos"]["x"], s["pos"]["z"],
                     math.hypot(x - s["pos"]["x"], z - s["pos"]["z"]))
        while time.time() < t_end:
            s = await self.st()
            if not s.get("pos"):
                return False
            p = s["pos"]
            if math.hypot(x - p["x"], z - p["z"]) <= near:
                return True
            av = json.dumps([list(a) for a in avoid])
            plan = await self.js(f"__pkbot.path({x}, {z}, {{near: {max(0.8, near - 0.5)}, avoid: {av}}})")
            pts = plan.get("pts") or []
            if len(pts) < 2:
                log.info("WALK no path to %.1f,%.1f (reach %.1f)", x, z, plan.get("reach") or -1)
                return False
            res = await self._follow(pts[1:], t_end, sprint)
            if res is True:
                continue
            if res == "popup":
                return False
            replans += 1
            if isinstance(res, tuple):
                avoid.add(res)
            if replans > 8:
                log.info("WALK gave up after %d replans", replans)
                return False
        log.info("WALK timeout")
        return False

    async def _follow(self, pts, t_end, sprint):
        """True = path done; 'popup' = a menu/battle opened; (x,z) = blocked cell; False = stuck."""
        last_p, last_t = None, time.time()
        tried = 0
        try:
            for wx, wz, wy, swim in pts:
                turns = 0
                while True:
                    if time.time() > t_end:
                        return False
                    s = await self.st()
                    if s.get("menus") or s.get("battle") or s.get("busy"):
                        return "popup"
                    p = s["pos"]
                    dist = math.hypot(wx - p["x"], wz - p["z"])
                    if dist < 0.7:
                        break
                    want = math.atan2(-(wx - p["x"]), -(wz - p["z"]))
                    d = wrap(want - s["yaw"])
                    if dist < 1.5 and abs(d) > 1.2:
                        break                  # passed it already: don't spin round for it
                    if not swim and not s.get("swim") and p["y"] < wy - 1.5 and dist < 2.5:
                        # a ledge right in front that we are under (we fell or the plan was off)
                        log.info("WALK ledge above us (%.1f < %d) at %.1f,%.1f, avoiding it", p["y"], wy, wx, wz)
                        return (int(math.floor(wx)), int(math.floor(wz)))
                    if abs(d) > 0.25:
                        turns += 1
                        if turns > 4:
                            log.info("WALK turned %d times for one waypoint, replanning", turns)
                            return (int(math.floor(wx)), int(math.floor(wz)))
                        await self.hold(["KeyW", "ShiftLeft"], False)
                        await self.turn(d)
                        await asyncio.sleep(0.05)
                        continue
                    if abs(d) > 0.08:
                        await self.turn(d)
                    keys = ["KeyW"] + (["ShiftLeft"] if sprint and dist > 3 and not swim else [])
                    await self.hold(keys, True)
                    if not sprint or dist <= 3:
                        await self.b.key("ShiftLeft", False)
                    await asyncio.sleep(0.12)
                    now = time.time()
                    if last_p is None or math.hypot(p["x"] - last_p[0], p["z"] - last_p[1]) > 0.4:
                        last_p, last_t = (p["x"], p["z"]), now
                        continue
                    stuck = now - last_t
                    if stuck < 1.2:
                        continue
                    await self.hold(["KeyW", "ShiftLeft"], False)
                    tried += 1
                    log.info("STUCK %.1fs at %.1f,%.1f (try %d) -> next %.1f,%.1f",
                             stuck, p["x"], p["z"], tried, wx, wz)
                    if tried == 1:
                        await self.b.key("KeyW", True)
                        await self.b.tap("Space", 0.3)
                        await self.b.key("KeyW", False)
                    elif tried == 2:
                        if await self.clear_way(p, wx, wz):
                            last_t = time.time()
                            continue
                    elif tried == 3:
                        await self.unstick()
                    else:
                        return (int(math.floor(wx)), int(math.floor(wz)))
                    last_t = time.time()
        finally:
            await self.hold(["KeyW", "ShiftLeft"], False)
        return True

    async def clear_way(self, p, wx, wz):
        """Break natural blocks between us and the next waypoint, if the game allows it."""
        dx, dz = wx - p["x"], wz - p["z"]
        n = math.hypot(dx, dz) or 1
        cx, cz = int(math.floor(p["x"] + dx / n * 0.9)), int(math.floor(p["z"] + dz / n * 0.9))
        y0 = int(math.floor(p["y"]))
        broke = False
        for y in (y0 + 1, y0, y0 + 2):
            info = await self.js(f"__pkbot.canBreak({cx},{y},{cz})")
            if info.get("air"):
                continue
            if not info.get("ok"):
                log.info("BLOCK %s at %d,%d,%d not breakable here (rule=%s)", info.get("name"), cx, y, cz,
                         info.get("rule"))
                continue
            log.info("BREAK %s at %d,%d,%d to clear the way", info["name"], cx, y, cz)
            await self.select_tool_for(info["name"])
            if await self.break_block(cx, y, cz, timeout=6):
                broke = True
        return broke

    async def select_tool_for(self, name):
        n = name.lower()
        order = (["wooden_axe", "stone_axe", "iron_axe"] if ("log" in n or "leaves" in n or "wood" in n)
                 else ["wooden_shovel", "shovel"] if any(k in n for k in ("dirt", "grass", "sand", "gravel", "snow", "clay", "mud"))
                 else ["iron_pickaxe", "stone_pickaxe", "wooden_pickaxe"])
        for t in order:
            if await self.select_item(t):
                return t
        return None

    async def unstick(self):
        """Back off and sidestep, like a player wriggling out of a corner."""
        await self.b.key("KeyS", True)
        await asyncio.sleep(0.6)
        await self.b.key("KeyS", False)
        side = "KeyA" if int(time.time()) % 2 else "KeyD"
        await self.b.key(side, True)
        await self.b.tap("Space", 0.25)
        await asyncio.sleep(0.6)
        await self.b.key(side, False)

    # ------------------------------------------------------------------ acting
    async def select_item(self, item_id):
        """Put an item from the hotbar (slots 1-9) in hand. Returns False if not in the hotbar."""
        s = await self.st()
        slot = next((x["i"] for x in s["inv"] if x["id"] == item_id and x["i"] < 9), None)
        if slot is None:
            return False
        if s["sel"] != slot:
            await self.b.tap(f"Digit{slot + 1}")
            await asyncio.sleep(0.15)
        return True

    async def break_block(self, x, y, z, timeout=12):
        """Aim at a block and hit it until it is gone."""
        t_end = time.time() + timeout
        while time.time() < t_end:
            gone = await self.js(f"__pk.world.get({x},{y},{z}) === 0")
            if gone:
                return True
            if not await self.aim(x, y, z):
                return False
            await self.b.taps(10)
            await asyncio.sleep(0.05)
        return await self.js(f"__pk.world.get({x},{y},{z}) === 0")

    async def interact(self):
        await self.b.tap("KeyE")
        await asyncio.sleep(0.3)

    async def right_click(self):
        cx, cy = self.b.size[0] // 2, self.b.size[1] // 2
        for t in ("mousePressed", "mouseReleased"):
            await self.b.send("Input.dispatchMouseEvent", {"type": t, "x": cx, "y": cy, "button": "right",
                                                           "buttons": 2 if t == "mousePressed" else 0,
                                                           "clickCount": 1})
            await asyncio.sleep(0.08)

    async def till(self, n):
        """Till up to n patches of grass/dirt around us with the hoe (game farm.till).
        Returns how many patches became Farmland."""
        if not await self.select_item("hoe") and not await self.select_item("wooden_hoe"):
            return 0
        done = 0
        for c in (await self.js("__pkbot.tillable(4)"))[: n + 4]:
            if done >= n:
                break
            if c["d"] > 3.2 or c["d"] < 0.9:
                continue
            if not await self.aim(c["x"], c["y"], c["z"]):
                continue
            await self.right_click()
            await asyncio.sleep(0.35)
            name = await self.js(f"__pkbot.blockName(__pk.world.get({c['x']},{c['y']},{c['z']}))")
            if name == "Farmland":
                done += 1
        return done

    async def gather(self, item, n, radius=70, timeout=240):
        """Pick up n decorations that give `item` (walk next to each, press E when the game
        offers 'Pick up …'). Returns how many were picked."""
        t_end = time.time() + timeout
        got = 0
        skip = set()
        while got < n and time.time() < t_end:
            spots = [d for d in await self.js(f"__pkbot.decorFind('{item}', {radius})")
                     if (round(d["x"], 1), round(d["z"], 1)) not in skip]
            if not spots:
                break
            d = spots[0]
            key = (round(d["x"], 1), round(d["z"], 1))
            before = sum(x["n"] for x in (await self.st())["inv"] if x["id"] == item)
            if not await self.walk_to(d["x"], d["z"], near=1.0, timeout=60):
                skip.add(key)
                continue
            s = await self.st()
            if (s.get("focus") or {}).get("k") != "decor":
                await self.face_xz(d["x"], d["z"])
                await asyncio.sleep(0.3)
            await self.interact()
            await asyncio.sleep(0.5)
            after = sum(x["n"] for x in (await self.st())["inv"] if x["id"] == item)
            if after > before:
                got += after - before
            else:
                skip.add(key)
        return got

    async def emote(self, emote_id):
        """Hold G to open the emote wheel and click the emote's slot (its title is the label)."""
        await self.b.key("KeyG", True)
        await asyncio.sleep(0.35)
        slots = await self.js("Array.from(document.querySelectorAll('.hd-wheel.open .ws')).map(e => {"
                              " const r = e.getBoundingClientRect(); return {t: e.title, x: r.x + r.width/2, y: r.y + r.height/2}; })")
        hit = next((x for x in slots or [] if x["t"].lower() == emote_id.lower()), None)
        if hit:
            await self.b.click(hit["x"], hit["y"])
        await asyncio.sleep(0.2)
        await self.b.key("KeyG", False)
        await asyncio.sleep(2.4)          # let the emote play (wave lasts ~2.3 s)
        return bool(hit)

    async def pet_buddy(self):
        """Walk up to the buddy and press E when the game offers 'Pet'."""
        for _ in range(6):
            s = await self.st()
            if (s.get("focus") or {}).get("k") == "buddy":
                await self.interact()
                await asyncio.sleep(1.2)
                return True
            bud = await self.js("__pkbot.buddy()")
            if not bud:
                return False
            await self.face_xz(bud["x"], bud["z"])
            await self.b.key("KeyW", True)
            await asyncio.sleep(0.25)
            await self.b.key("KeyW", False)
            await asyncio.sleep(0.6)
        return False

    # ------------------------------------------------------------------ menus
    async def buttons(self, all_layers=False):
        return await self.js(f"__pkbot.buttons({'true' if all_layers else 'false'})")

    async def click_button(self, fid=None, text=None, wait=0.4):
        """Click a visible menu button by its data-fid (exact) or label (substring)."""
        for x in await self.buttons():
            if (fid and x["fid"] == fid) or (text and text.lower() in x["text"].lower()):
                if x["off"]:
                    return False
                await self.b.click(x["x"], x["y"])
                await asyncio.sleep(wait)
                return True
        return False

    async def close_menus(self, tries=5):
        for _ in range(tries):
            s = await self.st()
            if not s.get("menus"):
                return True
            if s.get("dlg"):
                await self.run_dialogue(timeout=30)
                continue
            await self.b.tap("Escape")
            await asyncio.sleep(0.4)
        return not (await self.st()).get("menus")

    # ------------------------------------------------------------------ talking
    async def go_to_npc(self, npc_id, timeout=240):
        """Walk next to an NPC (it may wander, so re-read its position) until the game
        focuses it (the 'E Talk to …' prompt)."""
        t_end = time.time() + timeout
        tries = 0
        while time.time() < t_end:
            npcs = await self.js("__pkbot.npcs()")
            n = next((x for x in npcs if x["id"] == npc_id), None)
            if not n:
                return False
            s = await self.st()
            if s.get("battle") or s.get("dlg") or s.get("menus"):
                return False                   # something else needs handling first
            f = s.get("focus") or {}
            if f.get("k") == "npc" and f.get("id") == npc_id:
                return True
            if not n.get("shown") and n["d"] < 30:
                # near enough to be drawn but hidden: asleep / indoors by their daily schedule
                log.info("NPC %s is not out right now (hour %.1f)", npc_id, s.get("hour") or -1)
                return False
            tries += 1
            if tries > 12:
                return False
            if n["d"] > 2.0:
                # long trips (across the sea) get time in proportion to the distance
                budget = max(120, n["d"] * 3)
                t_end = max(t_end, time.time() + budget)
                if not await self.walk_to(n["x"], n["z"], near=1.8, timeout=min(budget, t_end - time.time())):
                    await asyncio.sleep(0.5)
            else:
                await self.face_xz(n["x"], n["z"])
                await asyncio.sleep(0.3)
                s = await self.st()
                f = s.get("focus") or {}
                if f.get("k") == "npc" and f.get("id") == npc_id:
                    return True
                # step closer
                await self.b.key("KeyW", True)
                await asyncio.sleep(0.25)
                await self.b.key("KeyW", False)
        return False

    async def talk(self, npc_id, goals=None, want=None, timeout=120):
        """Talk to an NPC and run the whole conversation.
        `goals`: game dialogue actions to aim for, in order (e.g. ["heal"], ["battle:fern"]).
        Handing in ("turnIn:") and taking quests ("giveQuest:") are always wanted.
        `want`: label words, only used if the dialogue graph cannot be read."""
        if not await self.go_to_npc(npc_id):
            return None
        await self.interact()
        return await self.run_dialogue(goals=goals, want=want, timeout=timeout)

    async def run_dialogue(self, goals=None, want=None, timeout=90):
        transcript = []
        t_end = time.time() + timeout
        idle = 0
        want = [w.lower() for w in (want or [])]
        used_want = False
        goals = list(goals or []) + ["turnIn:", "giveQuest:"]
        used = set()          # (node, label) already answered: never loop on the same answer
        while time.time() < t_end:
            s = await self.st()
            d = s.get("dlg")
            if not d:
                if not s.get("busy") and not s.get("menus"):
                    idle += 1
                    if idle > 3:
                        break
                elif s.get("menus") and not s.get("busy"):
                    break         # a shop / menu opened at the end of the talk: caller handles it
                await asyncio.sleep(0.3)
                continue
            idle = 0
            if d["typing"]:
                await self.b.tap("Space")
                await asyncio.sleep(0.15)
                continue
            line = f"{d['speaker']}: {d['text']}"
            if not transcript or transcript[-1] != line:
                transcript.append(line)
                log.info("DLG %s", line[:160])
            if d["choices"]:
                ch = d["choices"]
                enabled = [i for i, c in enumerate(ch) if not c["off"]]
                plan = await self.js("__pkbot.dlgPlan()")
                idx = None
                why = ""
                if plan and plan.get("node") and len(plan["choices"]) == len(ch):
                    node = plan["node"]
                    pc = plan["choices"]
                    for goal in goals:
                        best = None
                        for i in enabled:
                            if (node, pc[i]["label"]) in used:
                                continue
                            if any(a.startswith(goal) for a in pc[i]["acts"]):
                                dist = 0
                            else:
                                ds = [d for a, d in pc[i]["reach"].items() if a.startswith(goal)]
                                if not ds or pc[i]["risky"]:
                                    continue
                                dist = min(ds)
                            if best is None or dist < best[0]:
                                best = (dist, i)
                        if best:
                            idx, why = best[1], f"{goal} in {best[0]} step(s)"
                            break
                    if idx is None:
                        ends = [i for i in enabled if pc[i]["ends"] and not pc[i]["acts"]]
                        safe = [i for i in enabled if not pc[i]["risky"]]
                        if ends:
                            idx, why = ends[-1], "ends talk"
                        elif safe:
                            idx, why = leave_choice([ch[i] for i in safe]), "leave (safe)"
                            idx = safe[idx] if idx < len(safe) else safe[-1]
                    if idx is not None:
                        used.add((node, pc[idx]["label"]))
                if idx is None:
                    match = [i for i in enabled if any(w in ch[i]["label"].lower() for w in want)]
                    if match and not used_want:
                        idx, why, used_want = match[0], "label match", True
                    elif len(enabled) == 1:
                        idx, why = enabled[0], "only answer"
                    else:
                        idx, why = leave_choice(ch), "leave"
                log.info("DLG why: %s", why)
                transcript.append(f"  -> {d['choices'][idx]['label']}")
                log.info("DLG choose %s  (of %s)", d["choices"][idx]["label"],
                         [c["label"] + (" [off]" if c["off"] else "") for c in d["choices"]])
                await self.b.tap(f"Digit{idx + 1}")
            else:
                await self.b.tap("Space")
            await asyncio.sleep(0.35)
        return transcript


NEGATIVE =("no", "not now", "later", "bye", "goodbye", "leave", "never mind", "nevermind", "cancel",
            "that's all", "thats all", "nothing", "see you", "all good", "i'm fine", "im fine")


def leave_choice(choices, avoid=None):
    """The option that ends the conversation (Goodbye, Nothing, That's all ...)."""
    enabled = [i for i, c in enumerate(choices) if not c["off"] and i != avoid]
    for i in enabled:
        t = choices[i]["label"].lower()
        if any(t.startswith(w) or f" {w}" in t for w in NEGATIVE):
            return i
    return enabled[-1] if enabled else (avoid or 0)
