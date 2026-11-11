"""GitHub Actions'ın çalıştırdığı dosya.

GOREV=dongu (varsayılan): seans boyunca kapanmadan çalışır.
    * Telegram mesajlarına birkaç saniye içinde cevap verir
    * 3 dakikada bir: sabah planı / elindeki hisselerin kontrolü / gün sonu raporu
    * 10 dakikada bir durumu (data/bot.db) depoya kaydeder
    * GitHub'ın 6 saatlik sınırına gelmeden kendini yeniden başlatır
GOREV=otomatik: bir kere kontrol edip çıkar.
GOREV=sabah / deneme: elle sabah planı / deneme planı.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import timedelta

import requests

from bot.config import load_config
from bot.engine import Engine
from bot.storage import Storage, now
from bot.telegram import Telegram
from main import hm, is_trading_day, make_broker, register_commands, setup_logging

log = logging.getLogger("bistbot")

LOOP_END = "18:25"          # bu saatten sonra döngü kapanır
MAX_RUN_MIN = 330           # GitHub iş sınırı 360 dk; öncesinde kendini yeniden başlatır
STEP_EVERY_SEC = 180
SAVE_EVERY_SEC = 600


def save_state(msg: str = "bot durumu") -> None:
    """data/bot.db'yi depoya kaydet (sadece GitHub Actions içinde)."""
    if not os.getenv("GITHUB_ACTIONS"):
        return
    cmds = [
        "git add -f data/bot.db",
        f'git diff --cached --quiet || git commit -q -m "{msg} $(date -u +%H:%M) [skip ci]"',
        "git pull -q --rebase -X theirs || true",
        "git push -q || true",
    ]
    for c in cmds:
        subprocess.run(c, shell=True, check=False)


def restart_self() -> None:
    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_TOKEN")
    if not (repo and token):
        return
    r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/bot.yml/dispatches",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"ref": "main", "inputs": {"gorev": "dongu"}}, timeout=30)
    log.info("Yeniden başlatma isteği: %s", r.status_code)


def setup():
    setup_logging()
    cfg = load_config()
    db = Storage(cfg.db_path)
    chat_id = cfg.telegram_chat_id or db.get("chat_id", "")
    tg = Telegram(cfg.telegram_token, chat_id)
    eng = Engine(cfg, db, make_broker(cfg, db), tg)
    register_commands(tg, eng, cfg)
    return cfg, db, tg, eng


def claim_if_needed(cfg, db, tg) -> bool:
    if tg.chat_id:
        return True
    me = requests.get(f"{tg.base}/getMe", timeout=30).json()
    if not me.get("ok"):
        log.error("Telegram token GEÇERSİZ: %s", me.get("description"))
        return False
    r = requests.get(f"{tg.base}/getUpdates", params={"timeout": 0}, timeout=30).json()
    for upd in r.get("result", []):
        cid = tg.claim_chat(upd)
        if cid:
            db.set("chat_id", cid)
            tg.send("🤝 Merhaba Mert! Bot sana bağlandı. Komutlar için /yardim")
            return True
    log.warning("Telegram'da bota /start yazılmasını bekliyorum.")
    return False


def scheduled_step(cfg, db, eng) -> None:
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
        for p in db.open_pendings():
            db.set_pending_status(p["id"], "EXPIRED")


def main() -> None:
    cfg, db, tg, eng = setup()
    if not cfg.telegram_token:
        log.warning("TELEGRAM_TOKEN girilmemiş (Settings > Secrets > Actions). Bekliyorum.")
        return
    if not claim_if_needed(cfg, db, tg):
        return

    gorev = os.getenv("GOREV", "otomatik")
    if os.getenv("NAKIT", "").strip():
        amount = float(os.getenv("NAKIT").replace(".", "").replace(",", "."))
        db.set("cash", amount)
        db.set(eng._day_key(), amount)
        log.info("Nakit %.0f olarak ayarlandı", amount)

    if gorev == "sabah":
        db.set("tg_offset", tg.poll_once(int(db.get("tg_offset", 0))))
        log.info("Sabah planı (elle)")
        eng.morning()
        db.set(f"done_m:{now().date().isoformat()}", True)
        gorev = "dongu"   # ardından döngüye devam et
    elif gorev == "deneme":
        log.info("Deneme sabah planı")
        if not eng.market_is_ok():
            tg.send("🧪 Not: gerçek planda bugün piyasa filtresi alıma izin vermezdi; "
                    "mesajı görmen için denemede kapattım.")
            cfg.raw["use_market_filter"] = False
        tg.send("🧪 <b>DENEME</b> — sabah gelecek planın örneği bu. Butonlara BASMA.")
        before = {p["id"] for p in db.open_pendings()}
        eng.morning()
        for p in db.open_pendings():
            if p["id"] not in before:
                db.set_pending_status(p["id"], "EXPIRED")
        db.set(f"morning_done:{now().date().isoformat()}", False)
        db.set(eng._day_key(), eng.cash())
        return

    if gorev != "dongu":
        db.set("tg_offset", tg.poll_once(int(db.get("tg_offset", 0))))
        scheduled_step(cfg, db, eng)
        return

    # ---------------- Kapanmadan çalışma döngüsü ----------------
    t0 = time.time()
    last_step = last_save = 0.0
    h, m = hm(LOOP_END)
    log.info("Döngü başladı")
    while True:
        t = now()
        end_today = t.replace(hour=h, minute=m, second=0, microsecond=0)
        if t >= end_today or not is_trading_day(cfg, t):
            log.info("Seans bitti, döngü kapanıyor")
            break
        if time.time() - t0 > MAX_RUN_MIN * 60:
            log.info("Süre sınırı yaklaştı, yeniden başlatılıyor")
            save_state()
            restart_self()
            return
        # Telegram: 25 sn uzun bekleme -> mesajlara anında cevap
        db.set("tg_offset", tg.poll_once(int(db.get("tg_offset", 0)), wait=25))
        if time.time() - last_step >= STEP_EVERY_SEC:
            try:
                scheduled_step(cfg, db, eng)
            except Exception as e:
                log.exception("Adım hatası")
                tg.send(f"⚠️ Bot hatası: {e} — çalışmaya devam ediyorum.")
            last_step = time.time()
        if time.time() - last_save >= SAVE_EVERY_SEC:
            save_state()
            last_save = time.time()
    save_state()


if __name__ == "__main__":
    main()
