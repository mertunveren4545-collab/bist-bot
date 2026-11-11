"""BIST Bot — başlatma dosyası.

Kullanım:
    python main.py            -> botu sürekli çalıştırır (zamanlayıcı + Telegram)
    python main.py --test     -> Telegram'a deneme mesajı yollar
    python main.py --tara     -> bir kez tarar, en iyi hisseleri ekrana yazar
    python main.py --sabah    -> sabah raporunu hemen gönderir
    python main.py --kontrol  -> seans içi kontrolü bir kez çalıştırır
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler

from bot.brokers.algolab import AlgolabBroker
from bot.brokers.midas_manual import MidasManualBroker
from bot.brokers.paper import PaperBroker
from bot.config import ROOT, load_config
from bot.engine import Engine, tl
from bot.news import NewsEngine
from bot.storage import Storage, now
from bot.telegram import Telegram

log = logging.getLogger("bistbot")


def setup_logging() -> None:
    (ROOT / "data").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = RotatingFileHandler(ROOT / "data" / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])
    logging.getLogger("yfinance").setLevel(logging.WARNING)


def make_broker(cfg, db):
    if cfg.mode == "midas":
        return MidasManualBroker(db)
    if cfg.mode == "algolab":
        return AlgolabBroker(cfg.algolab_api_key, cfg.algolab_username, cfg.algolab_password)
    return PaperBroker()


def hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def is_trading_day(cfg, d: datetime) -> bool:
    return d.weekday() < 5 and d.date().isoformat() not in set(cfg.raw.get("holidays", []) or [])


def register_commands(tg: Telegram, eng: Engine, cfg) -> None:
    @tg.command("start")
    @tg.command("yardim")
    def _help(args):
        return ("🤖 <b>BIST Bot komutları</b>\n"
                "/durum — mod, nakit, açık pozisyon\n"
                "/portfoy — hisseler, kâr/zarar, stoplar\n"
                "/tara — şimdi tara, en iyi 10 hisse\n"
                "/hisse KOD — tek hissenin puan detayı\n"
                "/haber KOD — son haberler ve skoru\n"
                "/sat KOD — pozisyonu hemen kapat\n"
                "/islemler — son 15 işlem\n"
                "/rapor — gün sonu raporunu şimdi gönder\n"
                "/fiyat NO FİYAT — Midas dolum fiyatını düzelt\n"
                "/nakit TUTAR — Midas'taki serbest bakiyeni bildir\n"
                "/aldim KOD LOT FİYAT — kendi aldığın hisseyi takibe ekle\n"
                "/sattim KOD FİYAT — kendi sattığın hisseyi kayıttan düş\n"
                "/durdur — yeni alımları durdur  |  /devam — tekrar başlat\n\n"
                "🪙 <b>Kripto (BIST kapalıyken)</b>\n"
                "/kripto — kripto durumu (BEKLE/SAT)\n"
                "/kriptonakit TUTAR — Midas Kripto bakiyen\n"
                "/kriptoislemler — son kripto işlemleri\n"
                "Normal yazı da olur: <code>BTC aldım 48 bin</code>, <code>ETH sattım</code>")

    @tg.command("durum")
    def _status(args):
        return (f"⚙️ Mod: <b>{cfg.mode}</b> | {'⏸ DURDURULDU' if eng.paused else '▶️ çalışıyor'}\n"
                f"Nakit: {tl(eng.cash())} | Toplam: {tl(eng.equity())}\n"
                f"Açık pozisyon: {len(eng.db.positions())}/{cfg.risk['max_open_positions']} | "
                f"Bugünkü işlem: {len(eng.db.trades_today())}/{cfg.risk['max_trades_per_day']}\n"
                f"Onay bekleyen: {len(eng.db.open_pendings())}")

    @tg.command("portfoy")
    def _pf(args):
        return eng.portfolio_text()

    @tg.command("tara")
    def _scan(args):
        tg.send("🔎 Taranıyor, 1-2 dakika sürebilir...")
        scores = eng.scan(refresh_news=True)
        th = cfg.strategy["buy_threshold"]
        lines = ["📊 <b>Anlık tarama</b>"]
        for s in scores[:10]:
            lines.append(f"{'🟢' if s.total >= th else '⚪'} <b>{s.symbol}</b> {s.total:.0f} — {tl(s.price)}")
        return "\n".join(lines)

    @tg.command("hisse")
    def _one(args):
        if not args:
            return "Kullanım: /hisse THYAO"
        code = args[0].upper()
        eng.refresh_news()
        from bot.strategy import score_symbol
        sc = score_symbol(code, eng.daily_fn(code), eng.news_scores.get(code, 0.0), cfg.strategy)
        if not sc:
            return f"{code} için veri bulunamadı"
        notes = "\n• ".join(sc.notes) if sc.notes else "-"
        return (f"🔍 <b>{code}</b> — {sc.total:.0f}/100 | {tl(sc.price)}\n{sc.summary()}\n"
                f"ATR: {sc.atr:.2f}\n• {notes}")

    @tg.command("haber")
    def _news(args):
        if not args:
            return "Kullanım: /haber THYAO"
        code = args[0].upper()
        items = eng.news.for_ticker(code)[:8]
        if not items:
            return f"{code} için son 48 saatte haber yok"
        agg = NewsEngine.aggregate(items)
        lines = [f"📰 <b>{code}</b> haber skoru: {agg:+.2f}"]
        for i in items:
            e = "🟢" if i.score > 0.1 else "🔴" if i.score < -0.1 else "⚪"
            lines.append(f"{e} <a href=\"{i.link}\">{i.title[:110]}</a>")
        return "\n".join(lines)

    @tg.command("sat")
    def _sell(args):
        if not args:
            return "Kullanım: /sat THYAO"
        pos = eng.db.position(args[0].upper())
        if not pos:
            return "Bu hisse portföyde yok"
        px = eng.prices_fn([pos["symbol"]]).get(pos["symbol"], pos["entry_price"])
        eng.sell(pos, px, "Manuel satış (/sat)", force=True)
        return None

    @tg.command("islemler")
    def _trades(args):
        ts = eng.db.all_trades()[-15:]
        if not ts:
            return "Henüz işlem yok"
        return "\n".join(f"{t['ts'][5:16]} {'🟢AL' if t['side'] == 'BUY' else '🔴SAT'} {t['symbol']} "
                         f"{t['qty']}@{t['price']:.2f}" + (f" K/Z {t['pnl']:+.0f}" if t['side'] == 'SELL' else "")
                         for t in ts)

    @tg.command("rapor")
    def _report(args):
        eng.eod()
        return None

    @tg.command("fiyat")
    def _price(args):
        if len(args) != 2:
            return "Kullanım: /fiyat NO FİYAT  (ör. /fiyat 12 285,50)"
        return eng.adjust_fill(int(args[0]), float(args[1].replace(",", ".")))

    @tg.command("nakit")
    def _cash(args):
        if not args:
            return f"Kayıtlı nakit: {tl(eng.cash())}. Değiştirmek için: /nakit 50000"
        amount = float(args[0].replace(".", "").replace(",", "."))
        eng.db.set("cash", amount)
        eng.db.set(eng._day_key(), amount)
        return f"💰 Nakit {tl(amount)} olarak kaydedildi. Lotları buna göre hesaplayacağım."

    @tg.command("aldim")
    def _bought(args):
        if len(args) != 3:
            return "Kullanım: /aldim THYAO 100 285,50"
        return eng.manual_buy(args[0].upper(), int(args[1]), float(args[2].replace(",", ".")))

    @tg.command("sattim")
    def _sold(args):
        if len(args) != 2:
            return "Kullanım: /sattim THYAO 295,25"
        return eng.manual_sell(args[0].upper(), float(args[1].replace(",", ".")))

    @tg.command("durdur")
    def _pause(args):
        eng.set_paused(True)
        return "⏸ Yeni alımlar durduruldu. Stop/hedef takibi devam ediyor. /devam ile aç."

    @tg.command("devam")
    def _resume(args):
        eng.set_paused(False)
        return "▶️ Bot tekrar alım yapabilir."

    from bot.textparse import parse

    def _text(text: str):
        q = parse(text, cfg.universe)
        sym = q["symbol"]
        derived = False
        if q["price"] is None and q["amount"] and q["qty"]:
            q["price"] = round(q["amount"] / q["qty"], 2)
            derived = True
        today = now().date().isoformat()
        todays = [p for p in eng.db.positions() if p["opened_at"].startswith(today)]
        # "aldım" / "sattım" tek başına: bekleyen tek sinyali onayla
        if q["action"] and not sym:
            pend = [p for p in eng.db.open_pendings() if p["side"] == q["action"]]
            if len(pend) == 1:
                if q["action"] == "BUY":
                    eng.update_pending_fill(pend[0]["id"], q["qty"], q["price"])
                return eng.confirm_pending(pend[0]["id"], pend[0]["message_id"])
            if q["action"] == "BUY" and len(todays) == 1 and (q["price"] or q["qty"]):
                return eng.correct_position(todays[0]["symbol"], q["qty"], q["price"])
            if q["action"] == "SELL" and len(eng.db.positions()) == 1:
                p0 = eng.db.positions()[0]
                px = q["price"] or eng.prices_fn([p0["symbol"]]).get(p0["symbol"])
                if px:
                    return eng.manual_sell(p0["symbol"], px)
            return "Hangi hisse? Örnek: <code>ALARK aldım 53 bin</code> veya <code>ALARK sattım 107,30</code>"
        positions = eng.db.positions()

        def plausible(px, ref):
            return px if px and ref and 0.85 * ref <= px <= 1.15 * ref else None

        # Hisse adı yok, al/sat yok -> elindeki hisse hakkında konuşuyor
        if not sym and not q["action"] and len(positions) == 1:
            p0 = positions[0]
            return eng.advice(p0["symbol"], plausible(q["price"], p0["entry_price"]))
        if not sym and not q["action"] and len(positions) > 1:
            return "\n\n".join(eng.advice(p["symbol"]) for p in positions)
        if not sym:
            return ("Anlayamadım 🙂 Şöyle yazabilirsin:\n"
                    "• <code>ALARK aldım 53 bin</code>\n• <code>THYAO 100 lot aldım 285,50</code>\n"
                    "• <code>ALARK sattım 107,30</code>\n• <code>ALARK</code> (durum/hedef sorar)\n"
                    "Tüm komutlar: /yardim")
        price = q["price"] or eng.prices_fn([sym]).get(sym)
        pos_now = eng.db.position(sym)
        # "YKBNK 1259 lot 38,10" gibi: eylem yazılmamış ama lot/tutar var
        if not q["action"] and (q["qty"] or q["amount"]):
            q["action"] = "BUY"
        if q["action"] == "BUY" and pos_now and pos_now["opened_at"].startswith(today):
            return eng.correct_position(sym, q["qty"] or (eng._qty(q["amount"] / price) if q["amount"] and price else None),
                                        None if derived else q["price"])
        if q["action"] == "BUY":
            if not price:
                return f"{sym} fiyatı bulunamadı, fiyatı da yaz: <code>{sym} aldım 100 lot 25,40</code>"
            # bekleyen sinyal varsa onu onayla
            for p in eng.db.open_pendings():
                if p["symbol"] == sym and p["side"] == "BUY":
                    qty = q["qty"] or (eng._qty(q["amount"] / price) if q["amount"] else None)
                    eng.update_pending_fill(p["id"], qty, price)
                    return eng.confirm_pending(p["id"], p["message_id"])
            qty = q["qty"] or (eng._qty(q["amount"] / price) if q["amount"] else None)
            if not qty:
                return f"Kaç lot aldın? Örnek: <code>{sym} aldım 500 lot {price:.2f}</code> veya <code>{sym} aldım 50 bin</code>"
            if eng.db.position(sym):
                return f"{sym} zaten takipte. Yeni alımı eklemek için önce <code>{sym} sattım</code> yaz."
            note = "" if q["price"] else f"\n(Fiyat yazmadığın için son fiyatı {tl(price)} aldım; farklıysa: <code>{sym} sattım</code> deyip tekrar gir.)"
            return eng.manual_buy(sym, qty, price) + daytrade_note() + note
        if q["action"] == "SELL":
            if not price:
                return f"Satış fiyatını da yaz: <code>{sym} sattım 107,30</code>"
            return eng.manual_sell(sym, price)
        # sadece hisse adı / soru: elimdeyse tavsiye
        pos = eng.db.position(sym)
        if pos:
            return eng.advice(sym, plausible(q["price"], pos["entry_price"]))
        if pos:
            px = price or pos["entry_price"]
            pct = (px / pos["entry_price"] - 1) * 100
            return (f"📌 <b>{sym}</b> {pos['qty']} lot | maliyet {tl(pos['entry_price'])} → {tl(px)} "
                    f"({pct:+.2f}%, ≈ {tl((px - pos['entry_price']) * pos['qty'])})\n"
                    + eng.levels_text(sym, pos["entry_price"], pos["qty"], pos["stop"], pos["target"])
                    + daytrade_note().lstrip("\n"))
        return _one([sym])

    def daytrade_note() -> str:
        if eng.daytrade:
            return f"\n⏰ Ne olursa olsun {cfg.raw.get('gunici', {}).get('close_at', '17:30')}'da sat (günlük al-sat)."
        return ""

    tg.text_handler = _text

    @tg.on_callback("ok")
    def _ok(arg, mid):
        return eng.confirm_pending(int(arg), mid)

    @tg.on_callback("no")
    def _no(arg, mid):
        return eng.reject_pending(int(arg), mid)


def run_forever(cfg, eng: Engine, tg: Telegram) -> None:
    sch = cfg.schedule
    done: set[str] = set()
    last_intraday = datetime.min.replace(tzinfo=now().tzinfo)
    tg.send(f"🚀 BIST Bot başladı. Mod: <b>{cfg.mode}</b>. Komutlar için /yardim")
    while True:
        try:
            t = now()
            today = t.date().isoformat()
            if is_trading_day(cfg, t):
                def at(key):
                    h, m = hm(sch[key])
                    return t.replace(hour=h, minute=m, second=0, microsecond=0)

                start, end = at("session_start"), at("session_end")
                if t >= at("morning_scan") and f"m{today}" not in done and t < end:
                    log.info("Sabah taraması")
                    eng.morning()
                    done.add(f"m{today}")
                if start <= t <= end and t - last_intraday >= timedelta(minutes=sch["intraday_every_min"]):
                    allow = t >= start + timedelta(minutes=sch.get("avoid_first_minutes", 0))
                    log.info("Seans içi kontrol (alım %s)", "açık" if allow else "kapalı")
                    eng.intraday(allow_buys=allow)
                    last_intraday = t
                if t >= at("eod_report") and f"e{today}" not in done:
                    log.info("Gün sonu raporu")
                    eng.eod()
                    done.add(f"e{today}")
        except Exception as e:
            log.exception("Döngü hatası")
            tg.send(f"⚠️ Bot hatası: {e}\nBot çalışmaya devam ediyor.")
        time.sleep(30)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--tara", action="store_true")
    ap.add_argument("--sabah", action="store_true")
    ap.add_argument("--kontrol", action="store_true")
    args = ap.parse_args()

    setup_logging()
    cfg = load_config()
    db = Storage(cfg.db_path)
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id)
    eng = Engine(cfg, db, make_broker(cfg, db), tg)

    if args.test:
        tg.send("✅ Merhaba Mert! BIST Bot Telegram bağlantısı çalışıyor.")
        print("Deneme mesajı gönderildi." if tg.enabled else ".env içinde TELEGRAM bilgileri eksik!")
        return
    if args.tara:
        for s in eng.scan()[:15]:
            print(f"{s.symbol:6} {s.total:5.1f}  {s.price:10.2f}  {s.summary()}  {'; '.join(s.notes)}")
        return
    if args.sabah:
        eng.morning()
        return
    if args.kontrol:
        eng.intraday()
        return

    if not tg.enabled:
        log.warning("Telegram ayarlı değil — mesajlar sadece ekrana/log'a yazılacak.")
    register_commands(tg, eng, cfg)
    tg.start_polling()
    run_forever(cfg, eng, tg)


if __name__ == "__main__":
    main()
