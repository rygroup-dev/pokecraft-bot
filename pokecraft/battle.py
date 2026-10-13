"""Turn-based duel driver. Reads the battle screen (messages, menu buttons, the game's own
"super effective" markers) and answers with number keys, like a player would."""
import asyncio
import json
import logging
import re
import time

log = logging.getLogger("battle")

READ = r"""(() => { const q = s => document.querySelector(s);
 const t = el => el ? el.innerText.trim().replace(/\s+/g, ' ') : null;
 const menu = Array.from(document.querySelectorAll('.bt-menu button, .bt-pop button')).map((b, i) => {
   const r = b.getBoundingClientRect();
   return { text: t(b), off: b.disabled || b.getAttribute('aria-disabled') === 'true' || b.classList.contains('off'),
            cls: String(b.className), w: r.width, x: r.x + r.width / 2, y: r.y + r.height / 2,
            eff: (b.querySelector('.bt-eff') || {}).title || '' };
 }).filter(b => b.w > 2);
 return JSON.stringify({ root: !!q('.bt-root'), msg: t(q('.bt-msg')), foe: t(q('.bt-foe')), me: t(q('.bt-me')),
                         pop: t(q('.bt-pop .bt-title, .bt-pop h2')), menu, on: !!(window.__pk && __pk.battleOn) });
})()"""


def _hp(text):
    m = re.search(r"HP\s*(\d+)\s*/\s*(\d+)", text or "")
    return (int(m.group(1)), int(m.group(2))) if m else (None, None)


def _num(text, key):
    m = re.search(key + r"\s*(\d+)", text or "")
    return int(m.group(1)) if m else 0


class Battle:
    def __init__(self, player):
        self.p = player
        self.b = player.b
        self.want_cookie = False
        self.weak = set()       # move names the game called "not very effective" in this duel
        self.last_move = None

    async def read(self):
        return json.loads(await self.b.eval(READ))

    async def run(self, befriend=False, timeout=240, heal_below=0.35):
        """Play the duel to the end. befriend=True: weaken the foe, then use Befriend (cookie).
        Returns (outcome_text, transcript)."""
        t_end = time.time() + timeout
        log_lines = []
        last_msg = None
        turns = 0
        while time.time() < t_end:
            r = await self.read()
            if not r["on"] and not r["root"]:
                break
            if r["msg"] and r["msg"] != last_msg:
                last_msg = r["msg"]
                log_lines.append(r["msg"])
                log.info("BATTLE %s", r["msg"][:140])
                m = re.match(r"^(?!Wild )\S+ used (.+?)!$", r["msg"])
                if m:
                    self.last_move = m.group(1)
                elif r["msg"].startswith("It's not very effective") and self.last_move:
                    self.weak.add(self.last_move)
            menu = r["menu"]
            if not menu:
                await self.b.tap("Space")
                await asyncio.sleep(0.45)
                continue
            labels = [m["text"] or "" for m in menu]
            if any(l.startswith("Fight") for l in labels):         # main menu
                turns += 1
                hp, mx = _hp(r["me"])
                foe_frac = None
                if befriend and turns >= 2 and any(l.startswith("Befriend") for l in labels):
                    i = next(i for i, l in enumerate(labels) if l.startswith("Befriend"))
                    if not menu[i]["off"]:
                        await self.b.tap(f"Digit{i + 1}")
                        await asyncio.sleep(0.8)
                        continue
                if hp is not None and mx and hp / mx < heal_below:
                    i = next(i for i, l in enumerate(labels) if l.startswith("Bag"))
                    await self.b.tap(f"Digit{i + 1}")
                    await asyncio.sleep(0.6)
                    continue
                await self.b.tap("Digit1")
                await asyncio.sleep(0.6)
                continue
            if any("Pow" in l or "Heals" in l or "Half damage" in l or "Attack up" in l for l in labels):
                await self.b.tap(f"Digit{self.pick_move(menu, self.weak) + 1}")
                await asyncio.sleep(0.8)
                continue
            slots = [m for m in menu if "×" in (m["text"] or "")]
            if slots:
                # Bag pop-up: slots are [berry, mega berry, cookie] (digits 1..3), plus a Back button
                counts = [_num(m["text"].replace("×", "x "), "x") for m in slots]
                want = "cookie" if befriend and self.want_cookie else "heal"
                self.want_cookie = False
                if want == "cookie" and len(counts) > 2 and counts[2] > 0:
                    key = "Digit3"
                elif counts and counts[0] > 0 and not slots[0]["off"]:
                    key = "Digit1"
                elif len(counts) > 1 and counts[1] > 0 and not slots[1]["off"]:
                    key = "Digit2"
                else:
                    key = "Escape"
                await self.b.tap(key)
                await asyncio.sleep(0.8)
                continue
            party = [m for m in menu if re.search(r"\d+/\d+ \d+$", m["text"] or "")]
            if party:
                # "Who will you send out?": each creature ends with "<hp>/<max> <key>". Send the
                # healthiest one that isn't fainted or already out, by its own key (not by position:
                # the Back button comes first)
                ok = [m for m in party if not m["off"] and "Tuckered" not in m["text"]
                      and "Active" not in m["text"] and _hp("HP " + m["text"].rsplit(" ", 2)[-2])[0]]
                best = max(ok, key=lambda m: int(re.search(r"(\d+)/\d+ \d+$", m["text"]).group(1)), default=None)
                key = f"Digit{best['text'].rsplit(' ', 1)[-1]}" if best else "Escape"
                log.info("BATTLE send out %s -> %s", best["text"] if best else "nobody", key)
                await self.b.tap(key)
                await asyncio.sleep(0.8)
                continue
            # unknown menu (e.g. after-battle choice): take the first enabled option
            i = next((i for i, m in enumerate(menu) if not m["off"]), 0)
            log.info("BATTLE menu %s -> %s", labels, labels[i] if labels else "")
            await self.b.tap(f"Digit{i + 1}")
            await asyncio.sleep(0.8)
        return (log_lines[-1] if log_lines else ""), log_lines

    @staticmethod
    def pick_move(menu, weak=()):
        best, best_score = 0, -1e9
        for i, m in enumerate(menu):
            if m["off"]:
                continue
            t = m["text"] or ""
            pw = _num(t, "Pow")
            acc = _num(t, "Acc") or 100
            if not pw:
                score = 1      # heal / guard / buff: last resort
            else:
                seen_weak = any(t.startswith(w) for w in weak)
                mult = 2.0 if "Super" in m["eff"] else 0.5 if ("Not very" in m["eff"] or seen_weak) else 1.0
                score = pw * mult * acc / 100
            if score > best_score:
                best, best_score = i, score
        return best
