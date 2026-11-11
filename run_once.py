"""GitHub Actions'ın her 15 dakikada bir çalıştırdığı dosya.

Her çalışmada:
  1. Telegram'dan gelen komut ve buton basışlarını işler
  2. Saate göre sabah planı / seans içi kontrol / gün sonu raporunu yapar
  3. Durum data/bot.db dosyasına yazılır (workflow bunu repoya kaydeder)
"""
from __future__ import annotations

import logging
from datetime import timedelta

from bot.config import load_config
from bot.engine import Engine
from bot.storage import Storage, now
from bot.telegram import Telegram
from main import hm, is_trading_day, make_broker, register_commands, setup_logging

log = logging.getLogger("bistbot")


def main() -> None:
    setup_logging()
    cfg = load_config()
    db = Storage(cfg.db_path)
    chat_id = cfg.telegram_chat_id or db.get("chat_id", "")
    tg = Telegram(cfg.telegram_token, chat_id)
    eng = Engine(cfg, db, make_broker(cfg, db), tg)
    register_commands(tg, eng, cfg)

    if not cfg.telegram_token:
        log.warning("TELEGRAM_TOKEN girilmemiş (Settings > Secrets > Actions). Bekliyorum.")
        return

    # Chat id girilmemişse: bota ilk /start yazan sahip olur
    if not chat_id:
        import requests

        me = requests.get(f"{tg.base}/getMe", timeout=30).json()
        if not me.get("ok"):
            log.error("Telegram token GEÇERSİZ: %s (token %d karakter, başı %s...)",
                      me.get("description"), len(cfg.telegram_token), cfg.telegram_token[:4])
            return
        log.info("Telegram bot: @%s", me["result"].get("username"))
        r = requests.get(f"{tg.base}/getUpdates", params={"timeout": 0}, timeout=30).json()
        if not r.get("ok"):
            log.error("getUpdates hatası: %s", r)
        log.info("Bekleyen mesaj sayısı: %d", len(r.get("result", [])))
        for upd in r.get("result", []):
            cid = tg.claim_chat(upd)
            if cid:
                db.set("chat_id", cid)
                tg.send("🤝 Merhaba Mert! Bot sana bağlandı. Komutlar için /yardim")
                break
        if not tg.chat_id:
            log.warning("Telegram'da bota /start yazılmasını bekliyorum.")
            return

    # 1) Telegram mesajları ve butonlar
    db.set("tg_offset", tg.poll_once(int(db.get("tg_offset", 0))))

    # Elle tetiklenen ayarlar
    import os

    if os.getenv("NAKIT", "").strip():
        amount = float(os.getenv("NAKIT").replace(".", "").replace(",", "."))
        db.set("cash", amount)
        db.set(eng._day_key(), amount)
        log.info("Nakit %.0f olarak ayarlandı", amount)
    if os.getenv("GOREV") == "sabah":
        log.info("Sabah planı (elle)")
        eng.morning()
        db.set(f"done_m:{now().date().isoformat()}", True)
        return
    if os.getenv("GOREV") == "deneme":
        log.info("Deneme sabah planı")
        if not eng.market_is_ok():
            tg.send("🧪 Not: BIST 100 şu an 50 günlük ortalamasının altında, yani gerçek planda "
                    "bot alım önermezdi. Mesajın nasıl görüneceğini görmen için bu denemede filtreyi kapattım.")
            cfg.raw["use_market_filter"] = False
        tg.send("🧪 <b>DENEME</b> — sabah gelecek planın örneği bu. "
                "Bugünün kapanış fiyatlarıyla hesaplandı, butonlara BASMA.")
        before = {p["id"] for p in db.open_pendings()}
        eng.morning()
        for p in db.open_pendings():
            if p["id"] not in before:
                db.set_pending_status(p["id"], "EXPIRED")
        db.set(f"morning_done:{now().date().isoformat()}", False)  # deneme gerçek günü etkilemesin
        db.set(eng._day_key(), eng.cash())
        return

    # 2) Zamanlanmış işler
    t = now()
    if not is_trading_day(cfg, t):
        return
    sch = cfg.schedule
    today = t.date().isoformat()

    def at(key: str):
        h, m = hm(sch[key])
        return t.replace(hour=h, minute=m, second=0, microsecond=0)

    start, end = at("session_start"), at("session_end")
    if at("morning_scan") <= t < end and not db.get(f"done_m:{today}"):
        log.info("Sabah planı")
        eng.morning()
        db.set(f"done_m:{today}", True)
    elif start <= t <= end + timedelta(minutes=5):
        log.info("Seans içi kontrol")
        eng.intraday(allow_buys=t >= start + timedelta(minutes=sch.get("avoid_first_minutes", 0)))
    if t >= at("eod_report") and not db.get(f"done_e:{today}"):
        log.info("Gün sonu raporu")
        eng.eod()
        db.set(f"done_e:{today}", True)
        # gün sonunda süresi dolmamış onayları kapat
        for p in db.open_pendings():
            db.set_pending_status(p["id"], "EXPIRED")


if __name__ == "__main__":
    main()
