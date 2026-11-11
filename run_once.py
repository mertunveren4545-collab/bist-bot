"""Botun çalıştığı ana dosya: BIST (gündüz) + Kripto (BIST kapalıyken).

GOREV=dongu (varsayılan): kapanmadan çalışır.
    * Telegram mesajlarına birkaç saniye içinde cevap verir
    * 3 dakikada bir: BIST ve kripto kontrolleri
    * 10 dakikada bir durumu şifreli olarak depoya kaydeder
    * GitHub'ın 6 saatlik sınırına gelmeden kendini yeniden başlatır
GOREV=otomatik: tek kontrol.  GOREV=sabah: BIST sabah planını şimdi gönder.
GOREV=kripto_deneme: kripto verisini dene + deneme planı.
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timedelta

import requests

from bot import crypto_data
from bot.brokers.midas_manual import MidasManualBroker
from bot.config import ROOT, load_config
from bot.engine import Engine
from bot.storage import Storage, now
from bot.telegram import Telegram
from bot.textparse import parse
from main import hm, make_broker, register_commands, setup_logging

log = logging.getLogger("bistbot")

MAX_RUN_MIN = 330
STEP_EVERY_SEC = 180
SAVE_EVERY_SEC = 600


# ------------------------------------------------------------------ takvim
def at(t: datetime, hhmm: str) -> datetime:
    h, m = hm(hhmm)
    return t.replace(hour=h, minute=m, second=0, microsecond=0)


def is_bist_day(cfg, t: datetime) -> bool:
    return t.weekday() < 5 and t.date().isoformat() not in set(map(str, cfg.raw.get("holidays") or []))


def is_half_day(cfg, t: datetime) -> bool:
    return t.date().isoformat() in set(map(str, cfg.raw.get("half_days") or []))


def bist_close(cfg, t: datetime) -> datetime:
    return at(t, cfg.raw.get("half_day_close", "12:40")) if is_half_day(cfg, t) else at(t, "18:10")


def bist_open(cfg, t: datetime) -> bool:
    return is_bist_day(cfg, t) and at(t, "09:40") <= t < bist_close(cfg, t)


def crypto_active(cfg, t: datetime) -> bool:
    return not bist_open(cfg, t)


def crypto_buy_window(cfg, kcfg, t: datetime) -> bool:
    if not crypto_active(cfg, t):
        return False
    hours = kcfg.raw.get("buy_hours", {})
    full_day = is_bist_day(cfg, t) and not is_half_day(cfg, t)
    a, b = hours.get("weekday" if full_day else "offday", ["18:05", "23:30"])
    if is_half_day(cfg, t):
        a = max(a, (bist_close(cfg, t) + timedelta(minutes=10)).strftime("%H:%M"))
    return at(t, a) <= t <= at(t, b)


def crypto_force_close(cfg, kcfg, t: datetime) -> bool:
    """Hafta içi sabah 09:00 – BIST açılışı arası: elde kalan kriptoyu sat."""
    return is_bist_day(cfg, t) and at(t, kcfg.raw.get("force_close_at", "09:00")) <= t < at(t, "09:40")


# ------------------------------------------------------------------ durum kaydı (şifreli)
STATE = ROOT / "data" / "state.enc"
DBS = ["data/bot.db", "data/kripto.db"]


def _key() -> str:
    return os.getenv("STATE_KEY") or os.getenv("TELEGRAM_TOKEN", "")


def restore_state() -> None:
    if STATE.exists() and _key():
        r = subprocess.run(f'openssl enc -d -aes-256-cbc -pbkdf2 -k "$K" -in "{STATE}" | tar -xz -C "{ROOT}"',
                           shell=True, env={**os.environ, "K": _key()})
        log.info("Durum geri yüklendi (%s)", "tamam" if r.returncode == 0 else "HATA")


def save_state(msg: str = "bot durumu") -> None:
    if not os.getenv("GITHUB_ACTIONS") or not _key():
        return
    files = " ".join(f for f in DBS if (ROOT / f).exists())
    if not files:
        return
    cmds = [
        f'cd "{ROOT}" && tar -cz {files} | openssl enc -aes-256-cbc -pbkdf2 -salt -k "$K" -out data/state.enc',
        f'cd "{ROOT}" && git rm -q --cached --ignore-unmatch {" ".join(DBS)}',
        f'cd "{ROOT}" && git add -f data/state.enc',
        f'cd "{ROOT}" && (git diff --cached --quiet || git commit -q -m "{msg} $(date -u +%H:%M) [skip ci]")',
        f'cd "{ROOT}" && (git pull -q --rebase -X theirs || true) && (git push -q || true)',
    ]
    for c in cmds:
        subprocess.run(c, shell=True, check=False, env={**os.environ, "K": _key()})


def restart_self() -> None:
    repo, token = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_TOKEN")
    if not (repo and token):
        return
    r = requests.post(f"https://api.github.com/repos/{repo}/actions/workflows/bot.yml/dispatches",
                      headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
                      json={"ref": "main", "inputs": {"gorev": "dongu"}}, timeout=30)
    log.info("Yeniden başlatma isteği: %s", r.status_code)


def _closed(df):
    """Son (henüz kapanmamış) saatlik mumu at."""
    return df.iloc[:-1] if df is not None and len(df) > 61 else df


# ------------------------------------------------------------------ kurulum
def setup():
    setup_logging()
    restore_state()
    cfg = load_config()
    db = Storage(cfg.db_path)
    chat_id = cfg.telegram_chat_id or db.get("chat_id", "")
    tg = Telegram(cfg.telegram_token, chat_id)
    eng = Engine(cfg, db, make_broker(cfg, db), tg)
    register_commands(tg, eng, cfg)
    bist_text = tg.text_handler

    def bist_close_check():  # arife: 12:20'de kapat
        t = now()
        if is_half_day(cfg, t):
            return t >= at(t, "12:20")
        return t >= at(t, cfg.raw.get("gunici", {}).get("close_at", "17:30"))
    eng.close_check = bist_close_check

    # ---- kripto motoru
    kcfg = load_config(ROOT / "kripto.yaml")
    kdb = Storage(ROOT / "data" / "kripto.db")
    ktg = Telegram(cfg.telegram_token, chat_id)
    keng = Engine(kcfg, kdb, MidasManualBroker(kdb), ktg,
                  daily_fn=lambda s: _closed(crypto_data.candles(s, "1h", 300)),
                  prices_fn=crypto_data.last_prices)
    keng.range_df_fn = lambda s: crypto_data.candles(s, "1d", 40, cache_sec=3600)
    keng.close_check = lambda: crypto_force_close(cfg, kcfg, now())
    keng.buy_window = lambda: crypto_buy_window(cfg, kcfg, now())

    dummy = Telegram("", "0")
    register_commands(dummy, keng, kcfg)
    kr_text = dummy.text_handler
    tg.callbacks["kok"] = dummy.callbacks["ok"]
    tg.callbacks["kno"] = dummy.callbacks["no"]

    @tg.command("kripto")
    def _k(args):
        pos = keng.db.positions()
        if not pos:
            st = ("alım saatinde" if keng.buy_window()
                  else "sadece takipte" if crypto_active(cfg, now()) else "kapalı (BIST açık)")
            return f"🪙 Kripto: elde coin yok. Bakiye {keng.cash():,.0f} ₺. Kripto botu şu an {st}."
        return "\n\n".join(keng.advice(p["symbol"]) for p in pos)

    @tg.command("kriptonakit")
    def _kc(args):
        if not args:
            return f"Kripto bakiyesi: {keng.cash():,.0f} ₺. Değiştirmek için: /kriptonakit 48000"
        amount = float(args[0].replace(".", "").replace(",", "."))
        keng.db.set("cash", amount)
        keng.db.set(keng._day_key(), amount)
        return f"🪙 Kripto bakiyesi {amount:,.0f} ₺ olarak kaydedildi."

    @tg.command("kriptotara")
    def _kscan(args):
        return crypto_scan_text(keng, kcfg)

    @tg.command("kriptoteshis")
    def _kdiag(args):
        t = now(); d = t.date().isoformat()
        return (f"buy_window={keng.buy_window()} close={keng.close_time_reached()} "
                f"morning_done={kdb.get('morning_done:' + d)} kplan={kdb.get('kplan:' + d)}\n"
                f"positions={kdb.positions()}\npendings={kdb.open_pendings()}\n"
                f"cash={keng.cash()} trades_today={len(kdb.trades_today())} last_rescan={kdb.get('last_rescan')}")

    @tg.command("kriptoislemler")
    def _kt(args):
        return dummy.commands["islemler"]([])

    def route(text: str):
        q = parse(text, cfg.universe + kcfg.universe)
        sym = q["symbol"]
        if sym in kcfg.universe:
            return kr_text(text)
        if sym in cfg.universe:
            return bist_text(text)
        kb = bool(keng.db.positions() or keng.db.open_pendings())
        bb = bool(eng.db.positions() or eng.db.open_pendings())
        if kb and not bb:
            return kr_text(text)
        if bb and not kb:
            return bist_text(text)
        return kr_text(text) if crypto_active(cfg, now()) else bist_text(text)

    tg.text_handler = route
    return cfg, db, tg, eng, kcfg, kdb, keng


def crypto_scan_text(keng, kcfg) -> str:
    """Şu an kripto için en uygun coinler + en iyisinin seviyeleri."""
    from html import escape
    from bot.engine import tl
    scores = keng.scan(refresh_news=False)
    if not scores:
        return "⚠️ Kripto verisi alınamadı."
    px = keng.prices_fn([s.symbol for s in scores])
    th = kcfg.strategy["buy_threshold"]
    lines = [f"🪙 <b>Kripto taraması — {now():%H:%M}</b>",
             f"Piyasa: {'✅ alıma uygun' if keng.market_is_ok() else '⛔ Bitcoin son 24 saatte sert düştü'} | eşik {th}", ""]
    best = None
    for sc in scores[:6]:
        df = keng.daily_fn(sc.symbol)
        chg = (float(df.Close.iloc[-1]) / float(df.Close.iloc[-25]) - 1) * 100 if df is not None and len(df) > 25 else 0
        rng = keng.avg_range_pct(sc.symbol)
        live = px.get(sc.symbol, sc.price)
        mark = "🟢" if sc.total >= th else "⚪"
        lines.append(f"{mark} <b>{sc.symbol}</b> {sc.total:.0f} puan — {tl(live)} | 24s {chg:+.1f}% | günlük oynaklık %{rng:.1f}"
                     + (f"\n    └ {escape('; '.join(sc.notes[:2])).replace('20 günlük', '20 saatlik')}" if sc.notes else ""))
        if best is None and sc.total >= th:
            best = (sc, live, rng)
    cash = keng.cash() if keng.cash() >= 100 else 48000.0
    if best:
        sc, live, rng = best
        qty = keng._qty(cash * 0.995 / live)
        stop, t1, tp = keng.day_levels(live, qty)
        t2 = keng.stretch_target(sc.symbol, live, t1)
        ok = keng.reachable(sc.symbol, tp)
        fee = 2 * kcfg.raw.get("gunici", {}).get("fee_pct", 0.15) / 100 * qty * live
        lines += ["", f"👉 <b>En uygun: {sc.symbol}</b> ({tl(cash)} ile ≈ {qty:.6g} {sc.symbol})",
                  f"🎯 1. hedef {tl(t1)} (net ≈ +{tl((t1 - live) * qty - fee)})",
                  f"🚀 2. hedef {tl(t2)} (net ≈ +{tl((t2 - live) * qty - fee)}, %{(t2 / live - 1) * 100:.1f})",
                  f"🛑 Zarar kes {tl(stop)} (≈ −{tl((live - stop) * qty + fee)})",
                  "✅ Hareket alanı hedef için yeterli." if ok else "⚠️ Bu coinin günlük hareketi hedef için dar, dikkat."]
    else:
        lines += ["", "📌 Şu an eşiği geçen coin yok — beklemek daha iyi."]
    lines.append("\nNot: Kripto planı her akşam 18:05'te kendiliğinden gelir.")
    return "\n".join(lines)


def claim_if_needed(db, tg) -> bool:
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
    return False


# ------------------------------------------------------------------ adımlar
def bist_step(cfg, db, eng) -> None:
    t = now()
    if not is_bist_day(cfg, t):
        return
    sch = cfg.schedule
    today = t.date().isoformat()
    start = at(t, sch["session_start"])
    end = min(at(t, sch["session_end"]), bist_close(cfg, t))
    if at(t, sch["morning_scan"]) <= t < end and not db.get(f"done_m:{today}"):
        log.info("BIST sabah planı")
        eng.morning()
        db.set(f"done_m:{today}", True)
    elif start <= t <= end + timedelta(minutes=5):
        eng.intraday(allow_buys=t >= start + timedelta(minutes=sch.get("avoid_first_minutes", 0)))
    report_at = end + timedelta(minutes=10) if is_half_day(cfg, t) else at(t, sch["eod_report"])
    if t >= report_at and not db.get(f"done_e:{today}"):
        log.info("BIST gün sonu raporu")
        eng.eod()
        db.set(f"done_e:{today}", True)
        for p in db.open_pendings():
            db.set_pending_status(p["id"], "EXPIRED")


def crypto_step(cfg, kcfg, kdb, keng) -> None:
    t = now()
    today = t.date().isoformat()
    if crypto_force_close(cfg, kcfg, t):
        if kdb.positions():
            keng.intraday(allow_buys=False)   # close_check -> SAT
        if not kdb.get(f"krep:{today}") and t >= at(t, "09:05"):
            if kdb.all_trades():
                keng.eod()
            kdb.set(f"krep:{today}", True)
            for p in kdb.open_pendings():
                kdb.set_pending_status(p["id"], "EXPIRED")
        return
    if not crypto_active(cfg, t):
        return
    if keng.buy_window() and not kdb.get(f"kplan:{today}"):
        log.info("Kripto planı")
        keng.morning()
        kdb.set(f"kplan:{today}", True)
        return
    keng.intraday(allow_buys=False)
    if not is_bist_day(cfg, t) and t >= at(t, "23:35") and not kdb.get(f"krep:{today}"):
        if kdb.trades_today():
            keng.eod()
        kdb.set(f"krep:{today}", True)


# ------------------------------------------------------------------ ana
def main() -> None:
    cfg, db, tg, eng, kcfg, kdb, keng = setup()
    if not cfg.telegram_token:
        log.warning("TELEGRAM_TOKEN girilmemiş (Settings > Secrets > Actions). Bekliyorum.")
        return
    if not claim_if_needed(db, tg):
        log.warning("Telegram'da bota /start yazılmasını bekliyorum.")
        return
    keng.tg.chat_id, keng.tg.enabled = tg.chat_id, True

    gorev = os.getenv("GOREV", "otomatik")
    if os.getenv("NAKIT", "").strip():
        amount = float(os.getenv("NAKIT").replace(".", "").replace(",", "."))
        db.set("cash", amount)
        db.set(eng._day_key(), amount)
    if os.getenv("MESAJ", "").strip():
        m = os.getenv("MESAJ").strip()
        if m.startswith("/"):
            fn = tg.commands.get(m[1:].split()[0].lower())
            reply = fn(m.split()[1:]) if fn else None
        else:
            reply = tg.text_handler(m)
        if reply:
            log.info("MESAJ cevabı:\n%s", reply)
            tg.send(reply)

    if gorev == "sabah":
        eng.morning()
        db.set(f"done_m:{now().date().isoformat()}", True)
        gorev = "dongu"
    elif gorev == "kripto_deneme":
        px = crypto_data.last_prices(kcfg.universe[:3])
        df = crypto_data.candles("BTC", "1h", 300)
        log.info("Kripto fiyatları: %s | BTC mum: %s", px, None if df is None else len(df))
        if not px or df is None:
            tg.send("⚠️ Kripto fiyat verisi alınamadı, kaynağı değiştirmem gerekiyor.")
            save_state()
            return
        tg.send("🧪 <b>KRİPTO DENEME</b> — akşam gelecek planın örneği. Butonlara BASMA.\n"
                + " | ".join(f"{k} {v:,.0f} ₺" for k, v in px.items()))
        saved_cash = keng.cash()
        if saved_cash < 100:
            keng.db.set("cash", 48000)
        kcfg.raw["use_market_filter"] = False
        keng.buy_window = lambda: True
        before = {p["id"] for p in kdb.open_pendings()}
        keng.morning()
        for p in kdb.open_pendings():
            if p["id"] not in before:
                kdb.set_pending_status(p["id"], "EXPIRED")
        keng.db.set("cash", saved_cash)
        kdb.set(f"morning_done:{now().date().isoformat()}", False)
        save_state()
        return

    if gorev != "dongu":
        db.set("tg_offset", tg.poll_once(int(db.get("tg_offset", 0))))
        bist_step(cfg, db, eng)
        crypto_step(cfg, kcfg, kdb, keng)
        save_state()
        return

    t0 = time.time()
    last_step = last_save = 0.0
    log.info("Döngü başladı")
    while True:
        if time.time() - t0 > MAX_RUN_MIN * 60:
            log.info("Süre sınırı yaklaştı, yeniden başlatılıyor")
            save_state()
            restart_self()
            return
        db.set("tg_offset", tg.poll_once(int(db.get("tg_offset", 0)), wait=25))
        if time.time() - last_step >= STEP_EVERY_SEC:
            for name, fn in (("BIST", lambda: bist_step(cfg, db, eng)),
                             ("Kripto", lambda: crypto_step(cfg, kcfg, kdb, keng))):
                try:
                    fn()
                except Exception as e:
                    log.exception("%s adım hatası", name)
                    tg.send(f"⚠️ {name} botu hatası: {e} — çalışmaya devam ediyorum.")
            last_step = time.time()
        if time.time() - last_save >= SAVE_EVERY_SEC:
            save_state()
            last_save = time.time()


if __name__ == "__main__":
    main()
