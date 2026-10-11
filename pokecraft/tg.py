"""Telegram dashboard: one message per screen, edited in place, inline buttons."""
import asyncio
import html
import logging
import os
import time
from collections import Counter, defaultdict
from datetime import datetime

from telegram import (BotCommand, InlineKeyboardButton as B, InlineKeyboardMarkup,
                      KeyboardButton, ReplyKeyboardMarkup, Update)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from .api import ApiError
from .engine import INVITE_TASKS, RARITY_ICON, SPOTS, Engine, fmt_price, rarity_rank

log = logging.getLogger("tg")

LINE = "━━━━━━━━━━━━━━━━━━"
REPLY_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("🏠 Dashboard"), KeyboardButton("🎮 Game"), KeyboardButton("🎣 Farm")],
     [KeyboardButton("🎒 Tas"), KeyboardButton("🛒 Market"), KeyboardButton("🏦 Bank")],
     [KeyboardButton("⚙️ Setelan")]],
    resize_keyboard=True, is_persistent=True, input_field_placeholder="Pilih menu…")
REPLY_ROUTES = {"🏠 Dashboard": "dash", "🎮 Game": "game", "🎣 Farm": "farm", "🎒 Tas": "bag",
                "🛒 Market": "market", "🏦 Bank": "bank", "⚙️ Setelan": "settings"}
STATUS = {"farming": "🟢 Farming", "paused": "⏸ Dijeda", "limit": "🌙 Limit harian habis",
          "starting": "⏳ Menyiapkan…"}


def esc(s):
    return html.escape(str(s if s is not None else "-"))


def num(n):
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return "-"


def short(addr):
    return f"{addr[:4]}…{addr[-4:]}" if addr and len(addr) > 10 else (addr or "-")


def bar(left, total, width=10):
    if not total:
        return ""
    fill = round(width * max(0, min(left, total)) / total)
    return "▰" * fill + "▱" * (width - fill)


def kb(rows):
    return InlineKeyboardMarkup([[b for b in r if b] for r in rows if any(r)])


def back(to="dash"):
    return [B("⬅️ Kembali", callback_data=f"nav:{to}"), B("🏠 Menu", callback_data="nav:dash")]


def onoff(v):
    return "✅ ON" if v else "⬜ OFF"


class TelegramUI:
    def __init__(self, token, owner_id: int, engine: Engine):
        self.owner = owner_id
        self.e = engine
        self.app = Application.builder().token(token).post_init(self._post_init).build()
        self.app.add_handler(CommandHandler(["start", "menu"], self.cmd_start))
        for cmd, screen in [("status", "dash"), ("game", "game"), ("farm", "farm"), ("tas", "bag"),
                            ("market", "market"), ("bank", "bank"), ("referral", "invite"),
                            ("karakter", "char"), ("setelan", "settings"), ("log", "log"),
                            ("help", "help")]:
            self.app.add_handler(CommandHandler(cmd, self._screen_cmd(screen)))
        self.app.add_handler(CallbackQueryHandler(self.on_button))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        self.engine_notify_ready = False
        self.pending = None   # waiting for typed input: dict(kind=..., ...)

    # ------------------------------------------------------------------ setup
    async def _post_init(self, app: Application):
        await app.bot.set_my_commands([
            BotCommand("menu", "🏠 Dashboard utama"),
            BotCommand("game", "🎮 Bot main game (quest, duel, crafting)"),
            BotCommand("farm", "🎣 Atur farming drop"),
            BotCommand("tas", "🎒 Item & jual ke market"),
            BotCommand("market", "🛒 Marketplace POKECRAFT"),
            BotCommand("bank", "🏦 Bank gold"),
            BotCommand("referral", "🤝 Undangan & hadiah"),
            BotCommand("karakter", "🧑 Info karakter"),
            BotCommand("setelan", "⚙️ Setelan otomatis"),
            BotCommand("log", "📜 Riwayat aktivitas"),
            BotCommand("help", "❓ Bantuan"),
        ])
        self.e.notify = self.notify
        self._refresher = asyncio.create_task(self._refresh_loop())
        await self.notify("🤖 <b>Pokecraft bot aktif.</b>\nKetik /menu untuk membuka dashboard.")

    def run(self):
        self.app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    async def notify(self, text):
        await self.app.bot.send_message(self.owner, text, parse_mode=ParseMode.HTML,
                                        disable_web_page_preview=True)

    def _allowed(self, update: Update):
        u = update.effective_user
        return u is not None and u.id == self.owner

    async def _deny(self, update: Update):
        if update.callback_query:
            await update.callback_query.answer("⛔ Bot ini privat.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(
                "⛔ Bot ini privat.\nMau punya sendiri? Jalankan bot ini dengan wallet & bot Telegram kamu.")

    # ------------------------------------------------------------------ entry points
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._allowed(update):
            return await self._deny(update)
        await update.message.reply_text("👋 Selamat datang di <b>Pokecraft Bot</b>.",
                                        parse_mode=ParseMode.HTML, reply_markup=REPLY_KB)
        await self.send_screen(update.effective_chat.id, "dash")

    def _screen_cmd(self, screen):
        async def h(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
            if not self._allowed(update):
                return await self._deny(update)
            await self.send_screen(update.effective_chat.id, screen)
        return h

    async def send_screen(self, chat_id, screen, arg=None):
        text, markup = await self.render(screen, arg)
        m = await self.app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML,
                                            reply_markup=markup, disable_web_page_preview=True)
        if screen == "dash":
            self.e.store.set("dash_msg", [chat_id, m.message_id])

    async def edit(self, q, text, markup):
        try:
            await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                                      disable_web_page_preview=True)
        except BadRequest as ex:
            if "not modified" not in str(ex).lower():
                raise

    async def _refresh_loop(self):
        while True:
            await asyncio.sleep(60)
            await self._auto_refresh()

    async def _auto_refresh(self):
        dm = self.e.store.get("dash_msg")
        if not dm:
            return
        try:
            text, markup = await self.render("dash")
            await self.app.bot.edit_message_text(text, chat_id=dm[0], message_id=dm[1],
                                                 parse_mode=ParseMode.HTML, reply_markup=markup,
                                                 disable_web_page_preview=True)
        except BadRequest as ex:
            if "not modified" not in str(ex).lower():
                self.e.store.set("dash_msg", None)
        except Exception as ex:  # noqa: BLE001
            log.debug("auto refresh: %s", ex)

    # ------------------------------------------------------------------ text input
    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if not self._allowed(update):
            return await self._deny(update)
        t = (update.message.text or "").strip()
        if t in REPLY_ROUTES:
            self.pending = None
            return await self.send_screen(update.effective_chat.id, REPLY_ROUTES[t])
        p, self.pending = self.pending, None
        if not p:
            return await update.message.reply_text("Pilih menu di bawah ya 👇", reply_markup=REPLY_KB)
        try:
            msg = await self._handle_input(p, t)
        except ValueError:
            msg = "❌ Format salah. Ulangi dari tombolnya ya."
        except ApiError as ex:
            msg = f"❌ {esc(ex.msg)}"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        await self.send_screen(update.effective_chat.id, p.get("back", "dash"), p.get("back_arg"))

    async def _handle_input(self, p, t):
        t = t.replace(",", ".")
        if p["kind"] == "price":
            price = float(t)
            if price <= 0:
                raise ValueError
            return await self._sell_group(p["key"], p["qty"], price)
        if p["kind"] == "deposit":
            _, msg = await self.e.deposit_pouch(int(float(t)))
            return msg
        if p["kind"] == "withdraw":
            _, msg = await self.e.withdraw_bank(int(float(t)))
            return msg
        if p["kind"] == "sellgold":
            parts = t.split()
            gold, price = int(float(parts[0])), float(parts[1])
            await self.e.sell_gold(gold, price)
            return f"✅ {num(gold)} gold di-list @ {fmt_price(price)} POKECRAFT"
        if p["kind"] == "setnum":
            v = float(t)
            self.e.store.set(p["key"], int(v) if p.get("int") else v)
            return "✅ Tersimpan."
        raise ValueError

    async def _sell_group(self, key, qty, price):
        items = [i for i in self.e.items if i.get("key") == key and not i.get("locked")]
        if qty != "all":
            items = items[:int(qty)]
        ok = fail = 0
        last = ""
        for it in items:
            try:
                await self.e.sell_item(it["id"], price)
                ok += 1
            except ApiError as ex:
                fail += 1
                last = ex.msg
                if ex.code in ("rate_limited", "forbidden", "market_disabled"):
                    break
        msg = f"🛒 {ok} item di-list @ <b>{fmt_price(price)}</b> POKECRAFT."
        if fail:
            msg += f"\n⚠️ {fail} gagal: {esc(last)}"
        return msg

    # ------------------------------------------------------------------ buttons
    async def on_button(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        if not self._allowed(update):
            return await self._deny(update)
        data = q.data or ""
        act, _, arg = data.partition(":")
        toast = None
        try:
            if act == "nav":
                screen, _, sarg = arg.partition("|")
                text, markup = await self.render(screen, sarg or None)
                self._track_dash(q, screen)
                await q.answer()
                return await self.edit(q, text, markup)
            toast, screen, sarg = await self._action(q, act, arg)
            text, markup = await self.render(screen, sarg)
            self._track_dash(q, screen)
            await q.answer(toast or "✅", show_alert=bool(toast and len(toast) > 60))
            await self.edit(q, text, markup)
        except ApiError as ex:
            await q.answer(f"❌ {ex.msg}"[:190], show_alert=True)
        except Exception as ex:  # noqa: BLE001
            log.exception("button %s", data)
            await q.answer(f"❌ Error: {ex}"[:190], show_alert=True)

    def _track_dash(self, q, screen):
        """Auto-refresh only the message that is showing the dashboard right now."""
        here = [q.message.chat_id, q.message.message_id]
        if screen == "dash":
            self.e.store.set("dash_msg", here)
        elif self.e.store.get("dash_msg") == here:
            self.e.store.set("dash_msg", None)

    async def _action(self, q, act, arg):
        s, e = self.e.store, self.e
        chat = q.message.chat_id
        if act == "farm":
            if arg == "toggle":
                s.set("farm_on", not s.get("farm_on"))
                e.event("▶️ Farming dinyalakan" if s.get("farm_on") else "⏸ Farming dijeda")
                return ("▶️ Farming jalan" if s.get("farm_on") else "⏸ Farming dijeda"), "dash", None
            if arg in ("toggle_f",):
                s.set("farm_on", not s.get("farm_on"))
                return None, "farm", None
            if arg.startswith("delay"):
                d = float(s.get("delay_s")) + (1 if arg.endswith("+") else -1)
                s.set("delay_s", max(6.5, min(60, d)))
                return f"Jeda {s.get('delay_s'):.1f} dtk", "farm", None
        if act == "spot":
            if arg not in SPOTS and arg not in ("rotate", "smart"):
                return "?", "farm", None
            s.set("spot_mode", arg)
            label = self._spot_label(arg)
            return f"📍 {label}", "farm", None
        if act == "refresh":
            await e.refresh(full=True)
            return "🔄 Data diperbarui", arg or "dash", None
        if act == "dep":
            if arg == "auto":
                _, msg = await e.deposit_pouch()
                return msg, "bank", None
            self.pending = {"kind": "deposit", "back": "bank"}
            await self.app.bot.send_message(chat, "✍️ Ketik jumlah gold kantong yang mau di-deposit (maks 1000/kali):")
            return "Ketik jumlahnya", "bank", None
        if act == "wd":
            self.pending = {"kind": "withdraw", "back": "bank"}
            await self.app.bot.send_message(chat, "✍️ Ketik jumlah gold bank yang mau ditarik ke kantong:")
            return "Ketik jumlahnya", "bank", None
        if act == "sellgold":
            p = await e.gold_price()
            hint = f"Harga termurah sekarang ≈ <b>{fmt_price(p)}</b> POKECRAFT / 100 gold." if p else "Belum ada listing gold lain."
            self.pending = {"kind": "sellgold", "back": "bank"}
            await self.app.bot.send_message(
                chat, f"✍️ Ketik: <code>jumlah_gold harga_total</code>\nContoh: <code>500 5</code> "
                      f"(500 gold dijual 5 POKECRAFT)\n{hint}", parse_mode=ParseMode.HTML)
            return "Ketik jumlah & harga", "bank", None
        if act == "sell":
            key, qty, price = arg.split("|")
            msg = await self._sell_group(key, qty, float(price))
            return msg.replace("<b>", "").replace("</b>", ""), "item", key
        if act == "price":
            key, qty = arg.split("|")
            self.pending = {"kind": "price", "key": key, "qty": qty, "back": "item", "back_arg": key}
            await self.app.bot.send_message(chat, "✍️ Ketik harga per item dalam POKECRAFT (contoh <code>12.5</code>):",
                                            parse_mode=ParseMode.HTML)
            return "Ketik harganya", "item", key
        if act == "cancel":
            await e.api.cancel_listing(arg)
            e.event(f"❌ Listing {arg} dibatalkan")
            await e.refresh()
            return "Listing dibatalkan", "market", None
        if act == "game" and e.game:
            if arg == "start":
                await e.game.start()
                return "▶️ Membuka game…", "game", None
            if arg == "stop":
                await e.game.stop()
                return "⏹ Game dihentikan", "game", None
            if arg == "shot":
                path = os.path.join(os.path.dirname(e.store.path), "shot.png")
                if await e.game.screenshot(path):
                    with open(path, "rb") as f:
                        await self.app.bot.send_photo(chat, f, caption=f"🎮 {e.game.brain.last_action[:150] if e.game.brain else ''}")
                    return "📸 Terkirim", "game", None
                return "Game belum terbuka", "game", None
        if act == "probe":
            ok = await e.probe_market()
            return ("🟢 Market buka" if ok else "🔴 Market masih ditutup server"), "market", None
        if act == "claim":
            ok, msg = await e.claim_invite(arg)
            return msg, "invite", None
        if act == "shared":
            await e.mark_shared()
            return "📣 Ditandai sudah share", "invite", None
        if act == "set":
            key, _, step = arg.partition("|")
            if step:
                cur = float(s.get(key))
                v = cur + float(step)
                if key == "keep_pouch":
                    v = max(0, int(v))
                elif key == "sell_undercut_pct":
                    v = max(0, min(50, int(v)))
                else:
                    v = max(0.01, round(v, 2))
                s.set(key, v)
            else:
                s.set(key, not s.get(key))
            return None, "settings", None
        return "?", "dash", None

    # ------------------------------------------------------------------ screens
    async def render(self, screen, arg=None):
        fn = getattr(self, f"scr_{screen}", self.scr_dash)
        try:
            return await fn(arg)
        except ApiError as ex:
            return (f"⚠️ <b>Server error</b>\n{esc(ex.msg)}\n\nCoba lagi sebentar.",
                    kb([[B("🔄 Coba lagi", callback_data=f"nav:{screen}")], back()]))

    def _spot_label(self, mode):
        if mode == "smart":
            return f"🧠 Pintar (terbaik: {SPOTS[self.e.best_spot()][2]})"
        if mode == "rotate":
            return "🔁 Rotasi semua spot"
        return SPOTS.get(mode, ("", "", mode))[2]

    async def scr_dash(self, _=None):
        e, s = self.e, self.e.store
        if e.account is None:
            await e.refresh(full=True)
        a, si, lim = e.account or {}, e.save_info, e.limits
        d = s.today()
        sol, tok = e.balances
        spot = self._spot_label(s.get("spot_mode"))
        nxt = max(0, int(e.next_roll_at - time.time()))
        st = STATUS.get(e.status, e.status)
        if e.status == "farming":
            st += f" · roll berikutnya {nxt} dtk"
        dl = lim.get("dropsLeftToday", 0)
        lines = [
            "🎮 <b>POKECRAFT BOT</b>",
            LINE,
            f"👤 <b>{esc(si.get('name') or 'Belum ada karakter')}</b>"
            + (f" · Lv {esc(si.get('lvl'))}" if si.get("lvl") else ""),
            f"👛 <code>{esc(short(e.api.wallet.address))}</code>",
            f"{st}",
            f"📍 {esc(spot)}",
            "",
            "💰 <b>Saldo</b>",
            f"├ 🏦 Gold bank      <b>{num(a.get('gold'))}</b>",
            f"├ 👛 Gold kantong   <b>{num(si.get('gems'))}</b>",
            f"├ 🎒 Item tradable  <b>{len(e.items)}</b>",
            f"├ ◎ SOL            <b>{'-' if sol is None else f'{sol:.4f}'}</b>",
            f"└ 🪙 POKECRAFT      <b>{'-' if tok is None else fmt_price(round(tok, 2))}</b>",
            "",
            "📊 <b>Sisa limit hari ini</b>",
            f"├ Drop     {bar(dl, 2000)} <b>{num(dl)}</b>/2.000",
            f"├ Rare     <b>{num(lim.get('rareLeftToday'))}</b>/3",
            f"└ Deposit  <b>{num(lim.get('depositLeftToday'))}</b>/3.000",
            "",
            "📈 <b>Hasil hari ini</b>",
            f"├ 🎲 Roll {num(d['rolls'])} · 🎁 Drop {num(d['drops'])} · ✨ Rare {num(d['rare'])}",
            f"└ 💰 Gold +{num(d['gold'])} · 🏦 Depo {num(d['deposited'])} · 🛒 List {num(d['listed'])}",
        ]
        if e.game is not None:
            gs = {"off": "⏹ mati", "starting": "⏳ membuka…", "playing": "🟢 main", "error": "⚠️ error"}[e.game.state]
            jl = ((e.game.last or {}).get("journey") or {}).get("line")
            lines += ["", f"🎮 <b>Main game</b>: {gs}" + (f" · <i>{esc(jl)}</i>" if jl else "")]
        if e.market_ok is False:
            lines += ["", "🔴 <b>Market ditutup server game</b> — item & gold aman, bot kabari kalau buka."]
        if e.last_error:
            lines += ["", f"⚠️ <i>{esc(e.last_error[:120])}</i>"]
        lines += ["", f"🕒 <i>update {datetime.now():%H:%M:%S}</i>"]
        farm_btn = B("⏸ Jeda Farming" if s.get("farm_on") else "▶️ Mulai Farming", callback_data="farm:toggle")
        return "\n".join(lines), kb([
            [farm_btn, B("🔄 Refresh", callback_data="refresh:dash")],
            [B("🎮 Main Game", callback_data="nav:game")],
            [B("🎣 Farm", callback_data="nav:farm"), B("🎒 Tas", callback_data="nav:bag"),
             B("🛒 Market", callback_data="nav:market")],
            [B("🏦 Bank", callback_data="nav:bank"), B("🤝 Referral", callback_data="nav:invite"),
             B("🧑 Karakter", callback_data="nav:char")],
            [B("⚙️ Setelan", callback_data="nav:settings"), B("📜 Log", callback_data="nav:log"),
             B("❓ Bantuan", callback_data="nav:help")],
        ])

    async def scr_game(self, _=None):
        g = self.e.game
        if g is None or not g.available:
            return ("🎮 <b>MAIN GAME</b>\n" + LINE + "\nBrowser Edge/Chrome tidak ditemukan di mesin ini, "
                    "jadi bot tidak bisa memainkan game (quest, duel, crafting).", kb([back()]))
        st = {"off": "⏹ Mati", "starting": "⏳ Membuka game (±2 menit)…", "playing": "🟢 Sedang main",
              "error": "⚠️ Error, coba lagi otomatis"}[g.state]
        s = g.last or {}
        br = g.brain
        lines = ["🎮 <b>MAIN GAME (browser)</b>", LINE, f"Status : <b>{st}</b>"]
        if g.error:
            lines.append(f"<i>{esc(g.error)}</i>")
        if g.state == "playing":
            mins = int((time.time() - g.started_at) // 60)
            lines.append(f"Jalan  : {mins} menit")
        if br:
            lines += [f"Aksi   : <i>{esc(br.last_action[:90])}</i>"]
        if s.get("pos"):
            j = s.get("journey") or {}
            lines += ["", f"👤 Trainer Lv <b>{esc(s.get('lvl'))}</b> · 👛 Gold kantong <b>{num(s.get('gems'))}</b>",
                      f"🧭 Journey {j.get('step', '-')}/{j.get('total', '-')}: {esc(j.get('line'))}", "",
                      "🐾 <b>Party</b>"]
            for c in s.get("party", []):
                lines.append(f"• {esc(c['species'])} Lv{c['level']} · HP {c['hp']}/{c.get('max') or '?'}")
            qs = [q for q in s.get("quests", []) if not q["id"].startswith("jr_")]
            if qs:
                lines += ["", "📜 <b>Quest aktif</b>"]
                for q in qs[:8]:
                    lines.append(f"• {esc(q['title'])} ({esc(q['phase'])})")
        if br:
            st2 = br.stats
            lines += ["", f"📊 Duel {st2['duels']} · Menang {st2['wins']} · Teman baru {st2['befriended']} · "
                          f"Quest {st2['quests']}"]
            if br.skipped:
                lines += ["", "⏭ <b>Dilewati (belum didukung)</b>"]
                for k, v in list(br.skipped.items())[:6]:
                    lines.append(f"• {esc(k)}: {esc(v)}")
        btn = (B("⏹ Stop main", callback_data="game:stop") if g.running
               else B("▶️ Mulai main", callback_data="game:start"))
        return "\n".join(lines), kb([[btn, B("📸 Screenshot", callback_data="game:shot")],
                                     [B("🔄 Refresh", callback_data="nav:game")], back()])

    async def scr_farm(self, _=None):
        s = self.e.store
        mode = s.get("spot_mode")
        lines = ["🎣 <b>FARMING DROP</b>", LINE,
                 "Bot melempar pancing / mengayun beliung / kapak ke server.",
                 "Server yang mengacak hadiahnya: item tradable dan kadang gold bank.", "",
                 f"Status : {'🟢 Jalan' if s.get('farm_on') else '⏸ Dijeda'}",
                 f"Jeda   : <b>{float(s.get('delay_s')):.1f}</b> dtk + acak (cooldown server ±6 dtk)",
                 f"Sisa   : <b>{num(self.e.limits.get('dropsLeftToday'))}</b> drop hari ini",
                 f"Mode   : <b>{esc(self._spot_label(mode))}</b>", "",
                 "<i>🧠 Pintar = coba semua spot dulu, lalu fokus ke spot dengan hasil terbanyak.</i>", "",
                 "📍 <b>Statistik per spot</b> (roll · drop · gold · nilai/roll)"]
        best = self.e.best_spot()
        for k, (_, _, label) in SPOTS.items():
            st = self.e.store.data["spots"].get(k, {})
            r = st.get("rolls", 0)
            drops = sum(v for kk, v in st.items() if kk in ("common", "uncommon", "rare", "epic", "legendary"))
            rare = sum(v for kk, v in st.items() if kk in ("rare", "epic", "legendary"))
            mark = "🏆 " if r and k == best else ("👉 " if mode == k else "")
            lines.append(f"{mark}{label}: {num(r)} · 🎁{drops} · ✨{rare} · 💰{num(st.get('gold', 0))}"
                         f" · {self.e.spot_value(k):.0f}")
        rows = [[B(("✅ " if mode == "smart" else "") + "🧠 Pintar", callback_data="spot:smart"),
                 B(("✅ " if mode == "rotate" else "") + "🔁 Rotasi", callback_data="spot:rotate")]]
        keys = list(SPOTS)
        for i in range(0, len(keys), 2):
            rows.append([B(("✅ " if mode == k else "") + SPOTS[k][2], callback_data=f"spot:{k}")
                         for k in keys[i:i + 2]])
        rows += [[B("➖ Jeda", callback_data="farm:delay-"),
                  B("⏸ Jeda farming" if s.get("farm_on") else "▶️ Mulai farming", callback_data="farm:toggle_f"),
                  B("➕ Jeda", callback_data="farm:delay+")], back()]
        return "\n".join(lines), kb(rows)

    def _groups(self):
        g = defaultdict(list)
        for it in self.e.items:
            g[it.get("key")].append(it)
        return sorted(g.items(), key=lambda kv: (-rarity_rank(kv[1][0].get("rarity")), kv[1][0].get("name", "")))

    async def scr_bag(self, _=None):
        groups = self._groups()
        lines = ["🎒 <b>TAS (item tradable di akun)</b>", LINE]
        if not groups:
            lines.append("Masih kosong. Biarkan bot farming dulu ya 🎣")
        rows = []
        for key, its in groups[:24]:
            it = its[0]
            locked = sum(1 for x in its if x.get("locked"))
            icon = RARITY_ICON.get(it.get("rarity"), "⚪")
            lines.append(f"{icon} <b>{esc(it.get('name'))}</b> ×{len(its)}"
                         + (f" (🔒{locked} di market)" if locked else "") + f" · <i>{esc(it.get('kind'))}</i>")
            rows.append(B(f"{icon} {it.get('name')} ×{len(its)}", callback_data=f"nav:item|{key}"))
        btns = [rows[i:i + 2] for i in range(0, len(rows), 2)]
        lines += ["", "Ketuk item untuk lihat harga & jual."]
        return "\n".join(lines), kb(btns + [[B("🔄 Refresh", callback_data="refresh:bag")], back()])

    async def scr_item(self, key):
        its = [i for i in self.e.items if i.get("key") == key]
        if not its:
            return "Item ini sudah tidak ada di tas.", kb([back("bag")])
        it = its[0]
        free = [i for i in its if not i.get("locked")]
        low, n = await self.e.market_price(key)
        sug = self.e.suggest_price(low)
        sources = Counter(i.get("source", "-") for i in its)
        lines = [f"{RARITY_ICON.get(it.get('rarity'), '⚪')} <b>{esc(it.get('name'))}</b>", LINE,
                 f"Rarity : {esc(it.get('rarity'))}",
                 f"Jenis  : {esc(it.get('kind'))}",
                 f"Jumlah : <b>{len(its)}</b> (bisa dijual {len(free)})",
                 f"Asal   : {esc(', '.join(f'{k}×{v}' for k, v in sources.items()))}", "",
                 "🛒 <b>Market</b>",
                 f"├ Listing lain : {n}",
                 f"├ Termurah     : <b>{fmt_price(low) if low else '-'}</b> POKECRAFT",
                 f"└ Saran harga  : <b>{fmt_price(sug) if sug else '-'}</b> POKECRAFT "
                 f"(-{self.e.store.get('sell_undercut_pct')}%)", "",
                 "<i>Fee market 5% dipotong dari hasil jual. Hasil jual langsung masuk wallet.</i>"]
        if self.e.market_ok is False:
            lines += ["", "🔴 <b>Market sedang ditutup server game</b>, jual nanti setelah buka."]
        rows = []
        if free and sug:
            rows.append([B(f"💸 Jual 1 @ {fmt_price(sug)}", callback_data=f"sell:{key}|1|{sug}"),
                         B(f"💸 Jual semua @ {fmt_price(sug)}", callback_data=f"sell:{key}|all|{sug}")])
        if free:
            rows.append([B("✏️ Harga sendiri (1)", callback_data=f"price:{key}|1"),
                         B("✏️ Harga sendiri (semua)", callback_data=f"price:{key}|all")])
        rows.append(back("bag"))
        return "\n".join(lines), kb(rows)

    async def scr_market(self, _=None):
        e = self.e
        rows_all = await e.api.all_listings("all", "price")
        mine = [x for x in rows_all if x.get("mine")]
        others = [x for x in rows_all if not x.get("mine")]
        gp = await e.gold_price()
        hist = (await e.api.history()).get("history", [])
        state = {True: "🟢 Buka", False: "🔴 Ditutup server", None: "⏳ Belum dicek"}[e.market_ok]
        lines = ["🛒 <b>MARKETPLACE</b>", LINE,
                 f"Status        : <b>{state}</b>"]
        if e.market_ok is False:
            lines += [f"<i>{esc(e.market_msg)}</i>",
                      "<i>Masalah ada di server game (token Token-2022 pump.fun belum didukung). "
                      "Bot cek ulang tiap 30 menit.</i>"]
        lines += [f"Total listing : <b>{len(rows_all)}</b>",
                 f"Gold termurah : <b>{fmt_price(gp) if gp else '-'}</b> POKECRAFT / 100 gold", "",
                 "🏷 <b>Termurah dari pemain lain</b>"]
        for x in others[:8]:
            what = f"🪙 {num(x['gold'])} gold" if x.get("kind") == "gold" else \
                f"{RARITY_ICON.get(x['item'].get('rarity'), '⚪')} {esc(x['item'].get('name'))}"
            lines.append(f"• {what} — <b>{esc(x['priceUi'])}</b>")
        lines += ["", f"📦 <b>Listing saya ({len(mine)})</b>"]
        btns = []
        for x in mine[:10]:
            what = f"{num(x['gold'])} gold" if x.get("kind") == "gold" else x["item"].get("name")
            lines.append(f"• {esc(what)} — <b>{esc(x['priceUi'])}</b>")
            btns.append([B(f"❌ Batalkan {what} @ {x['priceUi']}"[:60], callback_data=f"cancel:{x['id']}")])
        if not mine:
            lines.append("<i>Belum ada.</i>")
        lines += ["", f"🧾 Riwayat transaksi: <b>{len(hist)}</b>"]
        for h in hist[:5]:
            if not isinstance(h, dict):
                continue
            what = (h.get("item") or {}).get("name") if isinstance(h.get("item"), dict) else None
            what = what or (f"{num(h['gold'])} gold" if h.get("gold") else h.get("kind", ""))
            side = h.get("side") or h.get("role") or ("jual" if h.get("mine") else "")
            lines.append(f"• {esc(what)} — {esc(h.get('priceUi', ''))} {esc(side)}")
        return "\n".join(lines), kb(btns + [[B("🔄 Refresh", callback_data="refresh:market"),
                                              B("🩺 Cek market", callback_data="probe:")], back()])

    async def scr_bank(self, _=None):
        e = self.e
        await e.refresh()
        a, si, lim = e.account or {}, e.save_info, e.limits
        lines = ["🏦 <b>BANK GOLD</b>", LINE,
                 f"🏦 Gold bank    : <b>{num(a.get('gold'))}</b>  (bisa dijual di market)",
                 f"👛 Gold kantong : <b>{num(si.get('gems'))}</b>  (di dalam game)",
                 f"⬆️ Sisa deposit : <b>{num(lim.get('depositLeftToday'))}</b> hari ini (maks 1.000/kali)",
                 f"🔒 Disisakan    : <b>{num(e.store.get('keep_pouch'))}</b> gold di kantong (setelan)", "",
                 "Gold kantong didapat dari main game (quest, jual hasil panen/ikan, daily).",
                 "Hanya gold bank yang bisa dijual jadi POKECRAFT."]
        return "\n".join(lines), kb([
            [B("⬆️ Deposit otomatis", callback_data="dep:auto"), B("✏️ Deposit jumlah", callback_data="dep:custom")],
            [B("⬇️ Tarik ke kantong", callback_data="wd:custom"), B("💱 Jual gold", callback_data="sellgold:")],
            [B("🔄 Refresh", callback_data="refresh:bank")], back()])

    async def scr_invite(self, _=None):
        e = self.e
        try:
            inv = await e.load_invites()
        except ApiError as ex:
            inv = None
            err = ex.msg
        else:
            err = ""
        if not inv:
            return ("🤝 <b>REFERRAL</b>\n" + LINE + "\nBelum bisa dimuat. Akun ini perlu save game dulu: buka "
                    f"game sekali lewat link referral, buat karakter, lalu connect wallet.\n{esc(err)}",
                    kb([back()]))
        link = e.invite_link()
        state = {"todo": "⬜", "ready": "🎁", "claimed": "✅", "waiting": "⏳"}
        lines = ["🤝 <b>REFERRAL & UNDANGAN</b>", LINE,
                 f"Kode kamu : <code>{esc(inv.get('code'))}</code>",
                 f"Link      : {esc(link)}",
                 f"Diundang  : <b>{inv.get('invited', 0)}</b> · lolos syarat <b>{inv.get('qualified', 0)}</b> "
                 f"/ maks {inv.get('cap', 20)}",
                 f"Ref akun ini : {'✅ ' + esc(inv.get('refState')) if inv.get('referred') else '—'}", "",
                 "<i>Teman dihitung setelah main sampai Trainer Lv3 dan minimal 5 menit.</i>", "",
                 "🎯 <b>Tugas</b>"]
        btns = []
        for t in inv.get("tasks", []):
            rw = t.get("reward", {})
            items = ", ".join(f"{i['id']}×{i['n']}" for i in rw.get("items", []))
            prog = f" ({t.get('have', 0)}/{t['need']})" if t.get("need") else ""
            lines.append(f"{state.get(t['state'], '•')} {INVITE_TASKS.get(t['id'], t['id'])}{prog}"
                         f" → +{rw.get('gold', 0)} gold{', ' + esc(items) if items else ''}")
            if t["state"] == "ready":
                btns.append([B(f"🎁 Klaim: {INVITE_TASKS.get(t['id'], t['id'])}", callback_data=f"claim:{t['id']}")])
        if not inv.get("shared"):
            btns.append([B("📣 Saya sudah share link", callback_data="shared:")])
        return "\n".join(lines), kb(btns + [[B("🔄 Refresh", callback_data="nav:invite")], back()])

    async def scr_char(self, _=None):
        si = self.e.save_info
        if not si.get("exists"):
            return ("🧑 <b>KARAKTER</b>\n" + LINE + "\nBelum ada save online. Main sekali di browser, "
                    "connect wallet, lalu klik <b>Save progress online</b>.", kb([back()]))
        mins = int(si.get("play_sec") or 0) // 60
        lines = ["🧑 <b>KARAKTER</b>", LINE,
                 f"Nama     : <b>{esc(si.get('name'))}</b>",
                 f"Level    : <b>{esc(si.get('lvl'))}</b> (xp {esc(si.get('xp'))})",
                 f"Quest    : <b>{si.get('quests_done')}</b> selesai dari {si.get('quests_total')} dibuka",
                 f"Pokédex  : <b>{si.get('dex')}</b> spesies",
                 f"Main     : <b>{mins}</b> menit", "",
                 "🐾 <b>Party</b>"]
        for p in si.get("party", []):
            lines.append(f"• {esc(p.get('nick') or p.get('species'))} Lv{p.get('level')} · HP {p.get('hp')} · "
                         f"bond {p.get('bond')}")
        if not si.get("party"):
            lines.append("<i>Belum ada.</i>")
        lines += ["", "<i>Quest, tutorial & equipment dikerjakan di Fase B (bot main di browser).</i>"]
        return "\n".join(lines), kb([[B("🔄 Refresh", callback_data="refresh:char")], back()])

    async def scr_settings(self, _=None):
        s = self.e.store
        lines = ["⚙️ <b>SETELAN OTOMATIS</b>", LINE,
                 f"🏦 Auto deposit kantong → bank : {onoff(s.get('auto_deposit'))}",
                 f"🔒 Sisakan di kantong          : <b>{num(s.get('keep_pouch'))}</b> gold",
                 f"🎁 Auto klaim hadiah referral  : {onoff(s.get('auto_claim_invites'))}",
                 f"🛒 Auto jual item ke market    : {onoff(s.get('auto_sell'))}",
                 f"   ├ Lebih murah dari termurah : <b>{s.get('sell_undercut_pct')}%</b>",
                 f"   ├ Harga minimum             : <b>{fmt_price(s.get('sell_min_price'))}</b> POKECRAFT",
                 f"   └ Simpan item rare ke atas  : {onoff(s.get('sell_keep_rare'))}",
                 f"🔔 Notif drop rare             : {onoff(s.get('notify_rare'))}", "",
                 "<i>Auto jual cuma jalan kalau ada harga pembanding di market.</i>"]
        return "\n".join(lines), kb([
            [B(f"🏦 Auto deposit {onoff(s.get('auto_deposit'))}", callback_data="set:auto_deposit")],
            [B("➖100", callback_data="set:keep_pouch|-100"), B("🔒 Sisa kantong", callback_data="nav:settings"),
             B("➕100", callback_data="set:keep_pouch|100")],
            [B(f"🎁 Auto klaim {onoff(s.get('auto_claim_invites'))}", callback_data="set:auto_claim_invites")],
            [B(f"🛒 Auto jual {onoff(s.get('auto_sell'))}", callback_data="set:auto_sell"),
             B(f"💎 Simpan rare {onoff(s.get('sell_keep_rare'))}", callback_data="set:sell_keep_rare")],
            [B("➖1%", callback_data="set:sell_undercut_pct|-1"), B("📉 Diskon", callback_data="nav:settings"),
             B("➕1%", callback_data="set:sell_undercut_pct|1")],
            [B("➖0.5", callback_data="set:sell_min_price|-0.5"), B("🪙 Harga min", callback_data="nav:settings"),
             B("➕0.5", callback_data="set:sell_min_price|0.5")],
            [B(f"🔔 Notif rare {onoff(s.get('notify_rare'))}", callback_data="set:notify_rare")],
            back()])

    async def scr_log(self, _=None):
        rows = self.e.store.data["log"][-20:]
        lines = ["📜 <b>LOG AKTIVITAS</b>", LINE]
        for ts, text in reversed(rows):
            lines.append(f"<code>{datetime.fromtimestamp(ts):%H:%M}</code> {esc(text)}")
        if not rows:
            lines.append("<i>Belum ada aktivitas.</i>")
        return "\n".join(lines), kb([[B("🔄 Refresh", callback_data="nav:log")], back()])

    async def scr_help(self, _=None):
        lines = ["❓ <b>BANTUAN</b>", LINE,
                 "<b>Cara dapat POKECRAFT</b>",
                 "1️⃣ Bot farming drop (mancing/tambang/tebang) → item tradable + kadang gold bank.",
                 "2️⃣ Gold kantong dari main game → deposit ke bank (maks 3.000/hari).",
                 "3️⃣ Jual item / gold bank di Market → dibayar POKECRAFT ke wallet (fee 5%).",
                 "4️⃣ Ajak teman lewat link referral → hadiah gold & item.", "",
                 "<b>Limit harian server</b>: 2.000 drop, 3 rare, 3.000 deposit.", "",
                 "<b>Penting</b>",
                 "• Kalau kamu main di browser setelah bot deposit/klaim, pilih <b>Load online progress</b> "
                 "supaya save tidak bentrok.",
                 "• Jangan pernah bagikan file <code>.env</code> (isinya private key).", "",
                 "<b>Perintah</b>: /menu /farm /tas /market /bank /referral /karakter /setelan /log"]
        return "\n".join(lines), kb([back()])
