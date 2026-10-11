"""Small JSON state file: settings, daily stats and the event log."""
import json
import os
import threading
import time
from datetime import datetime

DEFAULTS = {
    "farm_on": True,
    "spot_mode": "smart",       # smart | rotate | fish:sea | mine:cave | chop:forest ...
    "delay_s": 7.0,             # server cooldown is ~6s, keep a margin
    "auto_deposit": True,
    "keep_pouch": 100,          # gold left in the pouch for seeds, healing, etc.
    "auto_claim_invites": True,
    "auto_sell": False,
    "sell_undercut_pct": 5,     # list this % below the cheapest same-item listing
    "sell_min_price": 1.0,      # never list below this many POKECRAFT
    "sell_keep_rare": True,     # keep rare+ drops, sell commons only
    "notify_rare": True,
    "dash_msg": None,           # [chat_id, message_id] of the pinned dashboard
}


class Store:
    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self.data = {"settings": dict(DEFAULTS), "days": {}, "log": [], "spots": {}}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8-sig") as f:  # tolerate a BOM from editors
                    loaded = json.load(f)
                self.data.update(loaded)
                self.data["settings"] = {**DEFAULTS, **loaded.get("settings", {})}
            except (OSError, ValueError):
                pass

    def save(self):
        with self._lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    # settings
    def get(self, key):
        return self.data["settings"].get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self.data["settings"][key] = value
        self.save()

    # daily stats
    def today(self):
        k = datetime.now().strftime("%Y-%m-%d")
        d = self.data["days"].setdefault(k, {"rolls": 0, "drops": 0, "gold": 0, "rare": 0,
                                             "deposited": 0, "listed": 0, "sold": 0.0})
        # keep 14 days only
        for old in sorted(self.data["days"])[:-14]:
            del self.data["days"][old]
        return d

    def bump(self, **kw):
        d = self.today()
        for k, v in kw.items():
            d[k] = d.get(k, 0) + v

    def spot_stat(self, spot, rarity, gold=0):
        s = self.data["spots"].setdefault(spot, {"rolls": 0})
        s["rolls"] += 1
        s["gold"] = s.get("gold", 0) + gold
        if rarity:
            s[rarity] = s.get(rarity, 0) + 1

    # event log
    def event(self, text):
        self.data["log"].append([time.time(), text])
        self.data["log"] = self.data["log"][-60:]
