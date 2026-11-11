"""Botun beyni: tarama, alım/satım kararları, risk kontrolleri, Telegram mesajları."""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timedelta
from html import escape

from . import data as market
from .news import NewsEngine
from .risk import Plan, exit_reason, position_plan, round_tick, tick, trail_stop
from .storage import Storage, now
from .strategy import Score, market_ok, score_symbol

log = logging.getLogger(__name__)


def tl(x: float) -> str:
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{s} ₺"


class Engine:
    def __init__(self, cfg, db: Storage, broker, tg, daily_fn=None, prices_fn=None, news=None):
        self.cfg = cfg
        self.db = db
        self.broker = broker
        self.tg = tg
        self.daily_fn = daily_fn or market.daily
        self.prices_fn = prices_fn or market.last_prices
        self.news = news or NewsEngine(cfg.news)
        self.scores: dict[str, Score] = {}
        self.news_scores: dict[str, float] = {}
        self._news_ts = 0.0
        self.start_cash = float(cfg.raw.get("starting_cash", 100000))
        self._signals_this_check = 0

    # ======================================================== yardımcılar
    @property
    def paused(self) -> bool:
        return bool(self.db.get("paused", False))

    def set_paused(self, v: bool) -> None:
        self.db.set("paused", v)

    def cash(self) -> float:
        return self.db.cash(self.start_cash)

    def free_cash(self) -> float:
        """Onay bekleyen alımların tutarı düşülmüş nakit."""
        reserved = sum(p["qty"] * p["price"] for p in self.db.open_pendings() if p["side"] == "BUY")
        return self.cash() - reserved

    def equity(self, prices: dict[str, float] | None = None) -> float:
        pos = self.db.positions()
        if prices is None:
            prices = self.prices_fn([p["symbol"] for p in pos]) if pos else {}
        val = sum(p["qty"] * prices.get(p["symbol"], p["entry_price"]) for p in pos)
        return self.cash() + val

    def _day_key(self) -> str:
        return f"day_start_equity:{now().date().isoformat()}"

    def day_start_equity(self, eq: float) -> float:
        v = self.db.get(self._day_key())
        if v is None:
            self.db.set(self._day_key(), eq)
            return eq
        return float(v)

    def _has_pending(self, symbol: str) -> bool:
        return any(p["symbol"] == symbol for p in self.db.open_pendings())

    # ======================================================== tarama
    def refresh_news(self, force: bool = False) -> None:
        if not force and time.time() - self._news_ts < 3600:
            return
        self.news.refresh_general()
        for sym in self.cfg.universe:
            self.news_scores[sym] = NewsEngine.aggregate(self.news.for_ticker(sym))
        self._news_ts = time.time()

    @property
    def daytrade(self) -> bool:
        return self.cfg.raw.get("trade_style") == "gunici"

    def _eff(self, price: float, atr_val: float) -> tuple[float, dict]:
        """Günlük al-satta ATR yerine yüzde stop/hedef kullan (mevcut risk fonksiyonlarına uyarlanır)."""
        if not self.daytrade:
            return atr_val, self.cfg.risk
        g = self.cfg.raw.get("gunici", {})
        sp, tp, tr = g.get("stop_pct", 1.5), g.get("target_pct", 2.5), g.get("trail_pct", 1.5)
        r = dict(self.cfg.risk, stop_loss_atr=1.0, take_profit_atr=tp / sp, trailing_stop_atr=tr / sp)
        return price * sp / 100, r

    def day_levels(self, price: float, qty: int) -> tuple[float, float, float]:
        """Günlük al-sat: hedef TL kâra göre kâr al / zarar kes. (stop, target, hedef_yüzde)"""
        g = self.cfg.raw.get("gunici", {})
        value = max(1.0, price * qty)
        if g.get("target_tl"):
            tp = g["target_tl"] / value * 100
        else:
            tp = g.get("target_pct", 1.2)
        tp = max(g.get("min_target_pct", 0.6), min(tp, g.get("max_target_pct", 2.0)))
        sp = tp * g.get("stop_ratio", 0.8)
        t = tick(price)
        target = math.ceil(price * (1 + tp / 100) / t) * t
        stop = math.floor(price * (1 - sp / 100) / t) * t
        return round(stop, 2), round(target, 2), tp

    def stretch_target(self, symbol: str, price: float, t1: float) -> float:
        """2. hedef: hisse güçlüyse buraya kadar bekletilir (en fazla %3, hissenin günlük oynaklığına göre)."""
        g = self.cfg.raw.get("gunici", {})
        cap = g.get("stretch_max_pct", 3.0)
        rng = self.avg_range_pct(symbol) * g.get("stretch_range_share", 0.8)
        pct = max((t1 / price - 1) * 100, min(cap, rng or cap))
        t = tick(price)
        return round(math.ceil(price * (1 + pct / 100) / t) * t, 2)

    def _t1_key(self, symbol: str) -> str:
        return f"t1:{symbol}"

    def avg_range_pct(self, symbol: str) -> float:
        """Son 20 günde günlük ortalama (en yüksek - en düşük) / açılış, %."""
        df = self.daily_fn(symbol)
        if df is None or len(df) < 21:
            return 0.0
        d = df.iloc[-21:-1]
        return float(((d["High"] - d["Low"]) / d["Open"]).mean() * 100)

    def reachable(self, symbol: str, target_pct: float) -> bool:
        need = self.cfg.raw.get("gunici", {}).get("range_multiple", 2.5)
        return self.avg_range_pct(symbol) >= target_pct * need

    def close_time_reached(self) -> bool:
        if not self.daytrade:
            return False
        h, m = map(int, self.cfg.raw.get("gunici", {}).get("close_at", "17:30").split(":"))
        t = now()
        return t >= t.replace(hour=h, minute=m, second=0, microsecond=0)

    @property
    def plan_mode(self) -> bool:
        return bool(self.cfg.raw.get("daily_plan", False))

    def scan(self, refresh_news: bool = True, symbols: list[str] | None = None) -> list[Score]:
        if refresh_news:
            self.refresh_news()
        out: list[Score] = []
        for sym in (symbols if symbols is not None else self.cfg.universe):
            df = self.daily_fn(sym)
            sc = score_symbol(sym, df, self.news_scores.get(sym, 0.0), self.cfg.strategy)
            if sc:
                self.scores[sym] = sc
                out.append(sc)
        out.sort(key=lambda s: s.total, reverse=True)
        return out

    def market_is_ok(self) -> bool:
        if not self.cfg.raw.get("use_market_filter", True):
            return True
        idx = self.daily_fn(self.cfg.raw.get("market_index", "XU100"))
        if self.daytrade:
            # Günlük al-sat: sadece BUGÜN endeks sert düşüyorsa alım yapma
            if idx is None or len(idx) < 2:
                return True
            chg = (float(idx["Close"].iloc[-1]) / float(idx["Close"].iloc[-2]) - 1) * 100
            return chg > -self.cfg.raw.get("gunici", {}).get("max_index_drop_pct", 1.0)
        return market_ok(idx)

    # ======================================================== kayıt
    def record_fill(self, side, symbol, qty, price, stop=None, target=None, reason="") -> float:
        pnl = 0.0
        if side == "BUY":
            self.db.add_cash(-qty * price, self.start_cash)
            self.db.open_position(symbol, qty, price, stop, target)
        else:
            pos = self.db.position(symbol)
            if not pos:
                return 0.0
            qty = min(qty, pos["qty"])
            pnl = (price - pos["entry_price"]) * qty
            self.db.add_cash(qty * price, self.start_cash)
            self.db.close_position(symbol)
        self.db.add_trade(symbol, side, qty, price, reason, pnl)
        return pnl

    # ======================================================== alım / satım
    def can_open_new(self) -> tuple[bool, str]:
        if self.paused:
            return False, "Bot durduruldu (/devam ile başlat)"
        r = self.cfg.risk
        open_buys = sum(1 for p in self.db.open_pendings() if p["side"] == "BUY")
        if len(self.db.positions()) + open_buys >= r["max_open_positions"]:
            return False, "Maksimum pozisyon sayısına ulaşıldı"
        if len(self.db.trades_today()) >= r["max_trades_per_day"]:
            return False, "Günlük işlem limiti doldu"
        eq = self.equity()
        start = self.day_start_equity(eq)
        if start and (start - eq) / start * 100 >= r["max_daily_loss_pct"]:
            return False, f"Günlük zarar limiti (%{r['max_daily_loss_pct']}) aşıldı"
        return True, ""

    def try_buy(self, sc: Score) -> str | None:
        if self.db.position(sc.symbol) or self._has_pending(sc.symbol):
            return None
        ok, why = self.can_open_new()
        if not ok:
            return why
        eq = self.equity()
        if self.daytrade:
            g = self.cfg.raw.get("gunici", {})
            slots = max(1, g.get("max_concurrent", 1) - len(self.db.positions())
                        - sum(1 for p in self.db.open_pendings() if p["side"] == "BUY"))
            budget = self.free_cash() * (1 - g.get("cash_buffer_pct", 0.5) / 100) / slots
            qty = int(budget // sc.price)
            plan = None
            if qty >= 1:
                stop, target, tp = self.day_levels(sc.price, qty)
                if not self.reachable(sc.symbol, tp):
                    return f"{sc.symbol}: günlük hareketi hedef için yetersiz"
                t2 = self.stretch_target(sc.symbol, sc.price, target)
                self.db.set(self._t1_key(sc.symbol), target)
                self.db.set(f"locked:{sc.symbol}", False)
                plan = Plan(qty, stop, t2, round(qty * sc.price, 2))
        else:
            a, r = self._eff(sc.price, sc.atr)
            plan = position_plan(sc.price, a, eq, self.free_cash(), r)
        if not plan:
            return f"{sc.symbol}: yeterli nakit/lot yok"
        reason = f"Puan {sc.total:.0f} — {'; '.join(sc.notes) or sc.summary()}"
        res = self.broker.buy(sc.symbol, plan.qty, sc.price, plan.stop, plan.target, reason)
        self._signals_this_check += 1
        if res.status == "FILLED":
            self.record_fill("BUY", sc.symbol, res.qty, res.price, plan.stop, plan.target, reason)
            self.tg.send(self._buy_text(sc, plan, res.price, filled=True))
        elif res.status == "PENDING":
            mid = self.tg.send(self._buy_text(sc, plan, sc.price, filled=False, pid=res.pending_id),
                               buttons=[[("✅ Midas'ta aldım", f"ok:{res.pending_id}"),
                                         ("❌ Geç", f"no:{res.pending_id}")]])
            if mid:
                self.db.set_pending_message(res.pending_id, mid)
        else:
            self.tg.send(f"⚠️ {sc.symbol} alım emri reddedildi: {escape(res.message)}")
        return None

    def _snooze_key(self, symbol: str) -> str:
        return f"sell_snooze:{symbol}:{now().date().isoformat()}"

    def sell(self, pos: dict, price: float, reason: str, force: bool = False) -> None:
        if self._has_pending(pos["symbol"]):
            return
        if not force and self.db.get(self._snooze_key(pos["symbol"])):
            return
        res = self.broker.sell(pos["symbol"], pos["qty"], price, reason)
        self._signals_this_check += 1
        if res.status == "FILLED":
            pnl = self.record_fill("SELL", pos["symbol"], res.qty, res.price, reason=reason)
            self.tg.send(self._sell_text(pos, res.price, pnl, reason, filled=True))
        elif res.status == "PENDING":
            pnl = (price - pos["entry_price"]) * pos["qty"]
            mid = self.tg.send(self._sell_text(pos, price, pnl, reason, filled=False, pid=res.pending_id),
                               buttons=[[("✅ Midas'ta sattım", f"ok:{res.pending_id}"),
                                         ("❌ Tutmaya devam", f"no:{res.pending_id}")]])
            if mid:
                self.db.set_pending_message(res.pending_id, mid)
        else:
            self.tg.send(f"⚠️ {pos['symbol']} satış emri reddedildi: {escape(res.message)}")

    # ======================================================== Midas onayları
    def confirm_pending(self, pid: int, message_id: int | None = None) -> str:
        p = self.db.pending(pid)
        if not p or p["status"] != "PENDING":
            return "Bu sinyal artık geçerli değil"
        self.db.set_pending_status(pid, "DONE")
        self.record_fill(p["side"], p["symbol"], p["qty"], p["price"], p["stop"], p["target"], p["reason"])
        word = "ALINDI" if p["side"] == "BUY" else "SATILDI"
        txt = (f"✅ <b>{p['symbol']}</b> {p['qty']} lot @ {tl(p['price'])} {word} "
               f"(kayıt no {pid}).\nFarklı fiyattan dolduysa: /fiyat {pid} YENİ_FİYAT")
        if p["side"] == "BUY":
            txt += self.limit_hint(p["symbol"], p["qty"], p["target"])
        elif self.daytrade:
            txt += "\n🔁 Uygun hisse çıkarsa birkaç dakika içinde yenisini önereceğim."
        if message_id:
            self.tg.edit(message_id, txt)
        return txt

    def reject_pending(self, pid: int, message_id: int | None = None) -> str:
        p = self.db.pending(pid)
        if not p or p["status"] != "PENDING":
            return "Bu sinyal artık geçerli değil"
        self.db.set_pending_status(pid, "REJECTED")
        if p["side"] == "SELL":
            self.db.set(self._snooze_key(p["symbol"]), True)
        if message_id:
            self.tg.edit(message_id, f"❌ {p['symbol']} sinyali geçildi.")
        return "Geçildi"

    def adjust_fill(self, pid: int, new_price: float) -> str:
        p = self.db.pending(pid)
        if not p or p["status"] != "DONE":
            return "Önce sinyali ✅ ile onayla"
        diff = new_price - p["price"]
        if p["side"] == "BUY":
            pos = self.db.position(p["symbol"])
            if not pos:
                return "Pozisyon bulunamadı"
            self.db.add_cash(-diff * p["qty"], self.start_cash)
            self.db._exec("UPDATE positions SET entry_price=entry_price+?, stop=stop+?, target=target+?, "
                          "highest=MAX(highest, ?) WHERE symbol=?",
                          (diff, diff, diff, new_price, p["symbol"]))
        else:
            self.db.add_cash(diff * p["qty"], self.start_cash)
            self.db._exec("UPDATE trades SET pnl=pnl+? WHERE id=(SELECT MAX(id) FROM trades WHERE symbol=? "
                          "AND side='SELL')", (diff * p["qty"], p["symbol"]))
        self.db._exec("UPDATE trades SET price=? WHERE id=(SELECT MAX(id) FROM trades WHERE symbol=? AND side=?)",
                      (new_price, p["symbol"], p["side"]))
        self.db._exec("UPDATE pending SET price=? WHERE id=?", (new_price, pid))
        return f"{p['symbol']} fiyatı {tl(new_price)} olarak düzeltildi"

    def manual_buy(self, symbol: str, qty: int, price: float) -> str:
        df = self.daily_fn(symbol)
        sc = score_symbol(symbol, df, self.news_scores.get(symbol, 0.0), self.cfg.strategy) if df is not None else None
        atr_val = sc.atr if sc else price * 0.03
        if self.daytrade:
            stop, t1 = self.day_levels(price, qty)[:2]
            target = self.stretch_target(symbol, price, t1)
            self.db.set(self._t1_key(symbol), t1)
            self.db.set(f"locked:{symbol}", False)
        else:
            a, r = self._eff(price, atr_val)
            stop = round_tick(price - r["stop_loss_atr"] * a)
            target = round_tick(price + r["take_profit_atr"] * a)
        self.record_fill("BUY", symbol, qty, price, stop, target, "Manuel alım")
        return (f"✅ <b>{symbol}</b> {qty} lot @ {tl(price)} takibe alındı.\n"
                + self.levels_text(symbol, price, qty, stop, target)
                + self.limit_hint(symbol, qty, target))

    def levels_text(self, symbol: str, price: float, qty: int, stop: float, target: float) -> str:
        t1 = self.db.get(self._t1_key(symbol)) if self.daytrade else None
        if t1 and t1 < target:
            return (f"🎯 1. hedef: <b>{tl(t1)}</b> (≈ +{tl((t1 - price) * qty)}) — gelince satmıyoruz, kârı kilitliyoruz\n"
                    f"🚀 2. hedef: <b>{tl(target)}</b> (≈ +{tl((target - price) * qty)}, %{(target / price - 1) * 100:.1f})\n"
                    f"🛑 Zarar kes: {tl(stop)} (≈ −{tl((price - stop) * qty)})\n")
        return (f"🎯 Kâr al: <b>{tl(target)}</b> (≈ +{tl((target - price) * qty)})\n"
                f"🛑 Zarar kes: {tl(stop)} (≈ −{tl((price - stop) * qty)})\n")

    def correct_position(self, symbol: str, qty: int | None, price: float | None) -> str:
        """Gerçek dolum farklıysa pozisyonu (lot/fiyat) düzelt, seviyeleri yeniden hesapla."""
        pos = self.db.position(symbol)
        if not pos:
            return "Bu hisse takipte değil"
        qty = int(qty or pos["qty"])
        price = float(price or pos["entry_price"])
        old_cost = pos["qty"] * pos["entry_price"]
        self.db.add_cash(old_cost - qty * price, self.start_cash)
        if self.daytrade:
            stop, t1 = self.day_levels(price, qty)[:2]
            target = self.stretch_target(symbol, price, t1)
            self.db.set(self._t1_key(symbol), t1)
            self.db.set(f"locked:{symbol}", False)
        else:
            stop = round_tick(pos["stop"] + (price - pos["entry_price"]))
            target = round_tick(pos["target"] + (price - pos["entry_price"]))
        self.db._exec("UPDATE positions SET qty=?, entry_price=?, stop=?, target=?, highest=? WHERE symbol=?",
                      (qty, price, stop, target, price, symbol))
        self.db._exec("UPDATE trades SET qty=?, price=? WHERE id=(SELECT MAX(id) FROM trades WHERE symbol=? AND side='BUY')",
                      (qty, price, symbol))
        return (f"✏️ <b>{symbol}</b> düzeltildi: {qty} lot @ {tl(price)}\n"
                + self.levels_text(symbol, price, qty, stop, target)
                + self.limit_hint(symbol, qty, target))

    def update_pending_fill(self, pid: int, qty: int | None, price: float | None) -> None:
        """Onaydan önce gerçek fiyat/lotu yaz ve seviyeleri ona göre yeniden hesapla."""
        p = self.db.pending(pid)
        qty = int(qty or p["qty"])
        price = float(price or p["price"])
        stop, target = p["stop"], p["target"]
        if self.daytrade and p["side"] == "BUY":
            stop, t1 = self.day_levels(price, qty)[:2]
            target = self.stretch_target(p["symbol"], price, t1)
            self.db.set(self._t1_key(p["symbol"]), t1)
        self.db._exec("UPDATE pending SET qty=?, price=?, stop=?, target=? WHERE id=?",
                      (qty, price, stop, target, pid))

    def limit_hint(self, symbol: str, qty: int, target: float) -> str:
        if not self.daytrade:
            return ""
        return (f"\n\n👉 <b>Midas'ta 2. hedefe LİMİT SATIŞ gir:</b> {symbol} → Sat → Limit {tl(target)} → {qty} adet. "
                "Fiyat oraya fırlarsa Midas anında satar.\n"
                "1. hedef gelince sana yazıp zarar kesi kâra çekeceğim; fiyat geri dönerse "
                "'SAT' derim, kârın büyük kısmı cebinde kalır. Satınca bana 'sattım' yaz.")

    def manual_sell(self, symbol: str, price: float) -> str:
        pos = self.db.position(symbol)
        if not pos:
            return "Bu hisse takip listesinde yok"
        for p in self.db.open_pendings():
            if p["symbol"] == symbol:
                self.db.set_pending_status(p["id"], "DONE")
        pnl = self.record_fill("SELL", symbol, pos["qty"], price, reason="Manuel satış (/sattim)")
        return f"🔴 {symbol} satıldı olarak kaydedildi. K/Z: {tl(pnl)}"

    def expire_pendings(self) -> None:
        limit = timedelta(minutes=self.cfg.raw.get("midas_confirm_timeout_min", 30))
        for p in self.db.open_pendings():
            created = datetime.fromisoformat(p["created_at"])
            if now() - created > limit:
                self.db.set_pending_status(p["id"], "EXPIRED")
                if p["message_id"]:
                    self.tg.edit(p["message_id"], f"⌛ {p['symbol']} sinyalinin süresi doldu.")

    # ======================================================== zamanlanmış işler
    def morning(self) -> None:
        self.refresh_news(force=True)
        scores = self.scan(refresh_news=False)
        eq = self.equity()
        self.db.set(self._day_key(), eq)
        mood = self.news.market_mood()
        mkt = "✅ alıma uygun" if self.market_is_ok() else ("⛔ BIST 100 bugün sert düşüşte" if self.daytrade else "⛔ endeks zayıf, yeni alım yok")
        th = self.cfg.strategy["buy_threshold"]
        top_n = 5 if self.plan_mode else 10
        lines = [f"☀️ <b>Günaydın Mert — {now():%d.%m.%Y} BIST planı</b>",
                 f"Piyasa: {mkt} | Haber havası: {mood:+.2f}",
                 f"Sermaye: {tl(eq)} | Nakit: {tl(self.cash())}", "",
                 f"<b>En güçlü {top_n} hisse</b> (alım eşiği {th}):"]
        for s in scores[:top_n]:
            mark = "🟢" if s.total >= th else "⚪"
            lines.append(f"{mark} <b>{s.symbol}</b> {s.total:.0f} puan — {tl(s.price)}"
                         + (f"\n    └ {escape('; '.join(s.notes[:2]))}" if s.notes else ""))
        for s in scores:
            self.db.add_signal(s.symbol, s.total, {**s.parts, "notes": s.notes})
        if not self.plan_mode:
            self.tg.send("\n".join(lines))
            return
        # ---- Günlük plan: bugünün alımları sadece burada belirlenir ----
        buys = [s for s in scores if s.total >= th and not self.db.position(s.symbol)]
        max_new = int(self.cfg.raw.get("max_new_per_day", 2))
        if self.daytrade:
            max_new = int(self.cfg.raw.get("gunici", {}).get("max_concurrent", 1))
        ok, why = self.can_open_new()
        if not self.market_is_ok():
            lines += ["", "📌 <b>Bugün alım yok</b> — " + ("BIST 100 bugün sert düşüyor." if self.daytrade else "endeks zayıf.")
                      + " Sadece elindekileri takip ediyorum."]
            buys = []
        elif not ok:
            lines += ["", f"📌 <b>Bugün alım yok</b> — {why}."]
            buys = []
        elif not buys:
            lines += ["", "📌 <b>Bugün alım yok</b> — eşiği geçen hisse çıkmadı."]
        else:
            lines += ["", f"📌 <b>Bugünün alım planı: {min(len(buys), max_new)} hisse</b> (aşağıda). "
                          "Gün içinde başka alım önermeyeceğim, sadece satış uyarısı gelir."]
        self.tg.send("\n".join(lines))
        self.db.set(f"morning_done:{now().date().isoformat()}", True)
        sent = 0
        for sc in buys:
            if sent >= max_new:
                break
            msg = self.try_buy(sc)
            if not msg:
                sent += 1
            elif "limit" in msg or "durduruldu" in msg or "Maksimum" in msg:
                break

    def intraday(self, allow_buys: bool = True) -> None:
        self._signals_this_check = 0
        self._intraday(allow_buys)
        if self.cfg.raw.get("send_every_check", False) and self._signals_this_check == 0:
            self.tg.send(self.check_text(allow_buys), silent=True)

    def check_text(self, allow_buys: bool) -> str:
        th = self.cfg.strategy["buy_threshold"]
        top = sorted(self.scores.values(), key=lambda s: s.total, reverse=True)[:3]
        why = ""
        if not allow_buys:
            why = " (açılışın ilk dakikaları, alım bekleniyor)"
        elif not self.market_is_ok():
            why = " (endeks zayıf, yeni alım yok)"
        else:
            ok, reason = self.can_open_new()
            if not ok:
                why = f" ({reason})"
        lines = [f"⏱ <b>{now():%H:%M} kontrol</b> — yeni sinyal yok{why}",
                 "Eşiğe en yakın: " + ", ".join(f"{s.symbol} {s.total:.0f}" for s in top) + f" (eşik {th})"]
        pos = self.db.positions()
        if pos:
            prices = self.prices_fn([p["symbol"] for p in pos])
            for p in pos:
                px = prices.get(p["symbol"], p["entry_price"])
                lines.append(f"• {p['symbol']} {(px / p['entry_price'] - 1) * 100:+.2f}% | stop {tl(p['stop'])}")
        return "\n".join(lines)

    def _intraday(self, allow_buys: bool = True) -> None:
        self.expire_pendings()
        positions = self.db.positions()
        if self.plan_mode:
            allow_buys = False
            scores = self.scan(refresh_news=False, symbols=[p["symbol"] for p in positions])
        else:
            scores = self.scan(refresh_news=True)
        prices = self.prices_fn([p["symbol"] for p in positions]) if positions else {}
        # 1) Çıkışlar
        for pos in positions:
            price = prices.get(pos["symbol"])
            if price is None:
                continue
            sc = self.scores.get(pos["symbol"])
            if self.daytrade:
                atr_val, r = self._eff(pos["entry_price"], 0)
                sym, entry, qty = pos["symbol"], pos["entry_price"], pos["qty"]
                t1 = self.db.get(self._t1_key(sym)) or pos["target"]
                g = self.cfg.raw.get("gunici", {})
                be = entry + 0.6 * (t1 - entry)
                if price >= be and pos["stop"] < entry:
                    self.db.update_stop(sym, entry, max(pos["highest"], price))
                    pos = {**pos, "stop": entry, "highest": max(pos["highest"], price)}
                    self.tg.send(f"🔒 {sym} 1. hedefe yaklaştı, zarar kes alış fiyatına ({tl(entry)}) çekildi. "
                                 "Artık zararla çıkma ihtimalin yok.", silent=True)
                if price >= t1 and t1 < pos["target"]:
                    lock = round_tick(entry + g.get("lock_share", 0.65) * (t1 - entry))
                    hi = max(pos["highest"], price)
                    trail = round_tick(hi * (1 - g.get("trail_after_t1_pct", 0.6) / 100))
                    new_stop = max(pos["stop"], lock, trail)
                    if new_stop > pos["stop"]:
                        self.db.update_stop(sym, new_stop, hi)
                        pos = {**pos, "stop": new_stop, "highest": hi}
                    if not self.db.get(f"locked:{sym}"):
                        self.db.set(f"locked:{sym}", True)
                        self.tg.send(f"🎯 <b>{sym} 1. hedefi gördü</b> ({tl(price)}, ≈ +{tl((price - entry) * qty)})\n"
                                     f"Satmıyoruz, 2. hedef {tl(pos['target'])} için bekliyoruz.\n"
                                     f"🔒 Zarar kes {tl(new_stop)}'a çekildi → en kötü ≈ +{tl((new_stop - entry) * qty)} kârla çıkarsın.\n"
                                     "Fiyat yükseldikçe bu seviyeyi yukarı taşıyacağım.")
                    self._signals_this_check += 1
                    atr_val, r = 0.0, {**self.cfg.risk}
                    why = exit_reason(pos, price, None, 0, {**r, "max_holding_days": 10**6}, -1)
                    if why and price <= pos["stop"]:
                        why = f"Kâr kilidi — fiyat geri döndü (≈ +{tl((price - entry) * qty)})"
                    if not why and self.close_time_reached():
                        why = "Gün sonu — günlük al-satta pozisyon ertesi güne taşınmaz"
                    if why:
                        self.sell(pos, price, why, force=True)
                    continue
            else:
                r = self.cfg.risk
                atr_val = sc.atr if sc else (pos["entry_price"] - pos["stop"]) / r["stop_loss_atr"]
            new_stop, highest = trail_stop(pos, price, atr_val, r)
            if new_stop > pos["stop"] or highest > pos["highest"]:
                self.db.update_stop(pos["symbol"], new_stop, highest)
                pos = {**pos, "stop": new_stop, "highest": highest}
            days = (now() - datetime.fromisoformat(pos["opened_at"])).days
            if self.daytrade:
                why = exit_reason(pos, price, None, 0, {**r, "max_holding_days": 10**6}, -1)
                if why and price <= pos["stop"] and pos["stop"] > pos["entry_price"]:
                    why = f"Kâr kilidi — fiyat geri döndü (≈ +{tl((price - pos['entry_price']) * pos['qty'])})"
                elif why and price <= pos["stop"] and pos["stop"] == pos["entry_price"]:
                    why = "Başa baş — fiyat alış seviyesine döndü"
                force = False
                if not why and self.close_time_reached():
                    why, force = "Gün sonu — günlük al-satta pozisyon ertesi güne taşınmaz", True
                if why:
                    self.sell(pos, price, why, force=force)
                continue
            why = exit_reason(pos, price, sc.total if sc else None, days, r,
                              self.cfg.strategy["sell_threshold"])
            if why:
                self.sell(pos, price, why)
        # 2) Günlük al-sat: satılan hissenin yerine yeni hisse
        if self.daytrade and self.plan_mode:
            self.day_reentry()
            return
        # 2) Girişler
        if not allow_buys or not self.market_is_ok():
            return
        th = self.cfg.strategy["buy_threshold"]
        for sc in scores:
            if sc.total < th:
                break
            msg = self.try_buy(sc)
            if msg and ("limit" in msg or "durduruldu" in msg or "Maksimum" in msg):
                break

    def day_reentry(self) -> None:
        g = self.cfg.raw.get("gunici", {})
        h, m = map(int, g.get("reentry_until", "16:00").split(":"))
        t = now()
        if t >= t.replace(hour=h, minute=m, second=0, microsecond=0) or self.close_time_reached():
            return
        if not self.db.get(f"morning_done:{t.date().isoformat()}"):
            return
        busy = len(self.db.positions()) + sum(1 for p in self.db.open_pendings() if p["side"] == "BUY")
        if busy >= g.get("max_concurrent", 1):
            return
        bought_today = {x["symbol"] for x in self.db.trades_today() if x["side"] == "BUY"}
        buys_today = len(bought_today)
        if buys_today >= g.get("max_trades_per_day", 3) or not buys_today:
            return  # sabah hiç alım yapılmadıysa gün içinde de başlatma
        if not self.market_is_ok():
            return
        scores = self.scan(refresh_news=True)
        th = self.cfg.strategy["buy_threshold"]
        for sc in scores:
            if sc.total < th:
                break
            if sc.symbol in bought_today:
                continue
            self.tg.send("🔁 Önceki hisse satıldı, sıradaki fırsat:")
            msg = self.try_buy(sc)
            if not msg:
                return

    def eod(self) -> None:
        trades = self.db.trades_today()
        positions = self.db.positions()
        prices = self.prices_fn([p["symbol"] for p in positions]) if positions else {}
        eq = self.equity(prices)
        start = self.day_start_equity(eq)
        realized = sum(t["pnl"] for t in trades if t["side"] == "SELL")
        lines = [f"🌙 <b>Gün sonu raporu — {now():%d.%m.%Y}</b>",
                 f"Toplam değer: {tl(eq)} ({(eq - start) / start * 100 if start else 0:+.2f}% bugün)",
                 f"Başlangıçtan beri: {(eq - self.start_cash) / self.start_cash * 100:+.2f}%",
                 f"Gerçekleşen kâr/zarar bugün: {tl(realized)}", "",
                 f"<b>Bugünkü işlemler ({len(trades)})</b>"]
        for t in trades:
            icon = "🟢 AL" if t["side"] == "BUY" else "🔴 SAT"
            extra = f" | K/Z {tl(t['pnl'])}" if t["side"] == "SELL" else ""
            lines.append(f"{icon} {t['symbol']} {t['qty']} lot @ {tl(t['price'])}{extra}")
        lines += ["", self.portfolio_text(prices)]
        self.tg.send("\n".join(lines))

    # ======================================================== metinler
    def portfolio_text(self, prices: dict | None = None) -> str:
        pos = self.db.positions()
        if not pos:
            return f"📂 Portföy boş. Nakit: {tl(self.cash())}"
        if prices is None:
            prices = self.prices_fn([p["symbol"] for p in pos])
        lines = ["📂 <b>Portföy</b>"]
        for p in pos:
            px = prices.get(p["symbol"], p["entry_price"])
            pct = (px / p["entry_price"] - 1) * 100
            lines.append(f"{'📈' if pct >= 0 else '📉'} <b>{p['symbol']}</b> {p['qty']} lot | "
                         f"maliyet {tl(p['entry_price'])} → {tl(px)} ({pct:+.2f}%)\n"
                         f"    stop {tl(p['stop'])} | hedef {tl(p['target'])}")
        lines.append(f"Nakit: {tl(self.cash())} | Toplam: {tl(self.equity(prices))}")
        return "\n".join(lines)

    def _buy_text(self, sc: Score, plan, price, filled: bool, pid=None) -> str:
        head = "🟢 <b>ALINDI</b>" if filled else "🟢 <b>AL SİNYALİ — Midas'ta gir</b>"
        txt = (f"{head}: <b>{sc.symbol}</b>\n"
               f"Lot: <b>{plan.qty}</b> | Fiyat: <b>{tl(price)}</b> | Tutar: {tl(plan.qty * price)}\n"
               + self.levels_text(sc.symbol, price, plan.qty, plan.stop, plan.target) +
               f"Puan: {sc.total:.0f}/100 ({sc.summary()})")
        if sc.notes:
            txt += "\n• " + "\n• ".join(escape(n) for n in sc.notes)
        if self.daytrade:
            txt += f"\n⏰ Günlük al-sat: hedef/stop olmazsa {self.cfg.raw.get('gunici', {}).get('close_at', '17:30')}'da sat."
        if not filled:
            txt += (f"\n\nMidas → {sc.symbol} → Al → Limit {tl(price)} → {plan.qty} adet."
                    f"\nEmir gerçekleşince ✅'ye bas ya da 'aldım' yaz. (no {pid})")
        return txt

    def _sell_text(self, pos, price, pnl, reason, filled: bool, pid=None) -> str:
        head = "🔴 <b>SATILDI</b>" if filled else "🔴 <b>SAT SİNYALİ — Midas'ta sat</b>"
        pct = (price / pos["entry_price"] - 1) * 100
        txt = (f"{head}: <b>{pos['symbol']}</b> {pos['qty']} lot @ {tl(price)}\n"
               f"Sebep: {escape(reason)}\nKâr/Zarar: {tl(pnl)} ({pct:+.2f}%)")
        if not filled:
            txt += f"\n\nMidas → {pos['symbol']} → Sat → {pos['qty']} adet. Sonra ✅'ye bas. (no {pid})"
        return txt
