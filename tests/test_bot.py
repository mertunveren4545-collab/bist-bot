import numpy as np
import pandas as pd
import pytest

from bot.brokers.midas_manual import MidasManualBroker
from bot.brokers.paper import PaperBroker
from bot.config import load_config
from bot.engine import Engine
from bot.news import NewsEngine, parse_rss, score_text
from bot.risk import position_plan, round_tick, trail_stop
from bot.storage import Storage
from bot.strategy import score_symbol
from bot.telegram import Telegram


def make_df(n=300, drift=0.002, seed=1, last_vol_mult=2.0):
    rng = np.random.default_rng(seed)
    rets = drift + rng.normal(0, 0.012, n)
    close = 100 * np.exp(np.cumsum(rets))
    idx = pd.bdate_range("2024-01-01", periods=n)
    df = pd.DataFrame({"Open": close * 0.998, "High": close * 1.01, "Low": close * 0.99,
                       "Close": close, "Volume": rng.integers(1e6, 2e6, n).astype(float)}, index=idx)
    df.iloc[-1, df.columns.get_loc("Volume")] *= last_vol_mult
    df.iloc[-1, df.columns.get_loc("Close")] = df["Close"].iloc[-2] * 1.02
    return df


class FakeTG:
    def __init__(self):
        self.sent, self.edits = [], []

    def send(self, text, buttons=None, silent=False):
        self.sent.append((text, buttons, silent))
        return len(self.sent)

    def edit(self, mid, text):
        self.edits.append((mid, text))


class FakeNews:
    def refresh_general(self): return []
    def for_ticker(self, c): return []
    def market_mood(self): return 0.0


@pytest.fixture
def cfg():
    c = load_config()
    c.raw["universe"] = ["UPPP", "DOWN"]
    c.raw["use_market_filter"] = False
    c.raw["daily_plan"] = False
    return c


def build(cfg, tmp_path, broker_cls="paper"):
    db = Storage(tmp_path / "t.db")
    up, down = make_df(drift=0.003), make_df(drift=-0.003, seed=2)
    frames = {"UPPP": up, "DOWN": down}
    prices = {"UPPP": float(up.Close.iloc[-1]), "DOWN": float(down.Close.iloc[-1])}
    broker = PaperBroker() if broker_cls == "paper" else MidasManualBroker(db)
    tg = FakeTG()
    eng = Engine(cfg, db, broker, tg, daily_fn=lambda s: frames.get(s),
                 prices_fn=lambda syms: {s: prices[s] for s in syms if s in prices}, news=FakeNews())
    return eng, db, tg, prices


def test_score_uptrend_beats_downtrend(cfg):
    up = score_symbol("U", make_df(drift=0.003), 0.0, cfg.strategy)
    dn = score_symbol("D", make_df(drift=-0.003, seed=2), 0.0, cfg.strategy)
    assert up.total > dn.total
    assert 0 <= dn.total <= 100 and 0 <= up.total <= 100


def test_bad_news_blocks_buy(cfg):
    sc = score_symbol("U", make_df(drift=0.003), -0.9, cfg.strategy)
    assert sc.total < cfg.strategy["buy_threshold"]


def test_news_sentiment():
    assert score_text("THYAO rekor kâr açıkladı, pay geri alım programı") > 0.3
    assert score_text("Şirkete SPK soruşturma ve işlem yasağı") < -0.3
    assert score_text("Hava bugün güzel") == 0


def test_rss_parse():
    xml = """<rss><channel><item><title>ASELS yeni sözleşme imzaladı</title>
    <link>http://x</link><pubDate>Tue, 10 Nov 2026 09:00:00 +0300</pubDate></item></channel></rss>"""
    items = parse_rss(xml)
    assert len(items) == 1 and items[0].score > 0
    assert NewsEngine.aggregate(items) > 0


def test_tick_and_plan(cfg):
    assert round_tick(123.456) == 123.5
    assert round_tick(12.345) in (12.34, 12.35)
    plan = position_plan(100, 3, 100000, 100000, cfg.risk)
    assert plan.stop < 100 < plan.target
    assert plan.cost <= 100000 * cfg.risk["max_position_pct"] / 100 + 1
    # stop olursa kayıp ~%1
    assert (100 - plan.stop) * plan.qty <= 100000 * cfg.risk["risk_per_trade_pct"] / 100 + 1


def test_trailing_stop_only_moves_up(cfg):
    pos = {"stop": 90.0, "highest": 100.0}
    s1, h1 = trail_stop(pos, 120, 2, cfg.risk)
    assert s1 > 90 and h1 == 120
    s2, _ = trail_stop({"stop": s1, "highest": h1}, 110, 2, cfg.risk)
    assert s2 == s1


def test_paper_flow_buy_then_stop(cfg, tmp_path):
    eng, db, tg, prices = build(cfg, tmp_path)
    cfg.raw["strategy"]["buy_threshold"] = 1  # testte alımı garanti et
    eng.intraday()
    assert db.position("UPPP") is not None
    cash_after_buy = eng.cash()
    assert cash_after_buy < 100000
    # fiyat çöksün -> stop
    prices["UPPP"] = db.position("UPPP")["stop"] * 0.9
    prices["DOWN"] = prices["DOWN"]
    eng.intraday(allow_buys=False)
    assert db.position("UPPP") is None
    sells = [t for t in db.all_trades() if t["side"] == "SELL" and t["symbol"] == "UPPP"]
    assert sells and sells[0]["pnl"] < 0
    assert any("SATILDI" in m for m, *_ in tg.sent)


def test_midas_confirm_flow(cfg, tmp_path):
    eng, db, tg, prices = build(cfg, tmp_path, broker_cls="midas")
    cfg.raw["strategy"]["buy_threshold"] = 1
    eng.intraday()
    pend = db.open_pendings()
    assert pend, "Midas modunda onay bekleyen sinyal olmalı"
    assert db.positions() == []  # onaysız pozisyon açılmaz
    assert tg.sent[-1][1] is not None  # butonlu mesaj
    pid = pend[0]["id"]
    eng.confirm_pending(pid, 1)
    pos = db.position(pend[0]["symbol"])
    assert pos and pos["qty"] == pend[0]["qty"]
    # dolum fiyatı düzeltme
    old_cash = eng.cash()
    eng.adjust_fill(pid, pend[0]["price"] + 1)
    assert eng.cash() == pytest.approx(old_cash - pend[0]["qty"], abs=0.05)
    # ikinci kez onay işe yaramaz
    assert "geçerli değil" in eng.confirm_pending(pid, 1)


def test_pause_blocks_buys(cfg, tmp_path):
    eng, db, tg, _ = build(cfg, tmp_path)
    cfg.raw["strategy"]["buy_threshold"] = 1
    eng.set_paused(True)
    eng.intraday()
    assert db.positions() == []


def test_daily_loss_limit(cfg, tmp_path):
    eng, db, tg, _ = build(cfg, tmp_path)
    db.set(eng._day_key(), 200000)  # gün başı değer yüksek -> %50 kayıp gibi
    ok, why = eng.can_open_new()
    assert not ok and "zarar" in why


def test_telegram_ignores_strangers():
    tg = Telegram("", "111")
    called = []
    tg.commands["durum"] = lambda a: called.append(a) or None
    tg.handle_update({"message": {"chat": {"id": 999}, "text": "/durum"}})
    assert called == []
    tg.handle_update({"message": {"chat": {"id": 111}, "text": "/durum"}})
    assert called == [[]]


def test_backtest_runs(cfg):
    import backtest
    data = {"UPPP": make_df(400, 0.002), "DOWN": make_df(400, -0.002, seed=3)}
    res = backtest.run(data, cfg, 100000)
    assert res["son_deger"] > 0
    assert len(res["curve"]) > 100


def test_heartbeat_when_no_signal(cfg, tmp_path):
    cfg.raw["send_every_check"] = True
    cfg.raw["strategy"]["buy_threshold"] = 101  # hiç alım olmasın
    eng, db, tg, _ = build(cfg, tmp_path)
    eng.intraday()
    assert tg.sent and "kontrol" in tg.sent[-1][0] and tg.sent[-1][2] is True


def test_no_heartbeat_when_signal(cfg, tmp_path):
    cfg.raw["send_every_check"] = True
    cfg.raw["strategy"]["buy_threshold"] = 1
    eng, db, tg, _ = build(cfg, tmp_path, broker_cls="midas")
    eng.intraday()
    assert not any("kontrol" in m[0] for m in tg.sent)


def plan_engine(cfg, tmp_path, n_up=4, names=None):
    cfg.raw["daily_plan"] = True
    cfg.raw["trade_style"] = "swing"
    cfg.raw["max_new_per_day"] = 2
    cfg.raw["send_every_check"] = False
    cfg.raw["strategy"]["buy_threshold"] = 1
    names = names or [f"UP{i}" for i in range(n_up)]
    frames = {nm: make_df(drift=0.003, seed=10 + i) for i, nm in enumerate(names)}
    cfg.raw["universe"] = list(frames)
    prices = {k: float(v.Close.iloc[-1]) for k, v in frames.items()}
    db = Storage(tmp_path / "p.db")
    tg = FakeTG()
    eng = Engine(cfg, db, MidasManualBroker(db), tg, daily_fn=lambda s: frames.get(s),
                 prices_fn=lambda syms: {s: prices[s] for s in syms if s in prices}, news=FakeNews())
    return eng, db, tg, prices


def test_plan_morning_max_two_and_no_intraday_buys(cfg, tmp_path):
    eng, db, tg, prices = plan_engine(cfg, tmp_path)
    eng.morning()
    buys = [p for p in db.open_pendings() if p["side"] == "BUY"]
    assert len(buys) == 2
    # nakit rezerve edildi
    assert eng.free_cash() < eng.cash()
    n_before = len(tg.sent)
    for _ in range(4):
        eng.intraday()
    assert len([p for p in db.open_pendings() if p["side"] == "BUY"]) == 2
    assert len(tg.sent) == n_before  # gün içinde yeni alım mesajı yok


def test_plan_sell_alert_and_snooze(cfg, tmp_path):
    eng, db, tg, prices = plan_engine(cfg, tmp_path)
    eng.morning()
    pid = db.open_pendings()[0]["id"]
    sym = db.pending(pid)["symbol"]
    eng.confirm_pending(pid, 1)
    prices[sym] = db.position(sym)["stop"] * 0.95
    eng.intraday()
    sells = [p for p in db.open_pendings() if p["side"] == "SELL"]
    assert len(sells) == 1 and sells[0]["symbol"] == sym
    eng.reject_pending(sells[0]["id"], 2)
    eng.intraday()  # bugün tekrar sormamalı
    assert not [p for p in db.open_pendings() if p["side"] == "SELL"]


def test_manual_buy_sell(cfg, tmp_path):
    eng, db, tg, prices = plan_engine(cfg, tmp_path)
    msg = eng.manual_buy("UP0", 10, 100.0)
    assert "takibe" in msg and db.position("UP0")["qty"] == 10
    msg = eng.manual_sell("UP0", 110.0)
    assert db.position("UP0") is None and "100" in msg


def test_telegram_claim_chat():
    tg = Telegram("tok", "")
    assert tg.claim_chat({"message": {"chat": {"id": 555}, "text": "/start"}}) == "555"
    assert tg.chat_id == "555" and tg.enabled


GUN = {"target_tl": 650, "stop_ratio": 0.8, "min_target_pct": 0.6, "max_target_pct": 2.0,
       "range_multiple": 0.1, "max_concurrent": 1, "max_trades_per_day": 3, "reentry_until": "23:59",
       "cash_buffer_pct": 0.5, "stop_pct": 1.0, "trail_pct": 1.5, "close_at": "23:59"}


def test_daytrade_levels_and_close(cfg, tmp_path):
    eng, db, tg, prices = plan_engine(cfg, tmp_path)
    cfg.raw["trade_style"] = "gunici"
    cfg.raw["gunici"] = dict(GUN, close_at="00:00")
    eng.manual_buy("UP0", 500, 100.0)
    pos = db.position("UP0")
    t1 = db.get("t1:UP0")
    assert t1 == pytest.approx(101.3) and pos["stop"] == pytest.approx(98.9)
    assert (t1 - 100) * 500 >= 649.9
    assert t1 <= pos["target"] <= 103.0 + 1e-9  # 2. hedef en fazla %3
    prices["UP0"] = 100.5
    eng.intraday()
    sells = [p for p in db.open_pendings() if p["side"] == "SELL"]
    assert sells and "Gün sonu" in sells[0]["reason"]


def test_daytrade_full_day_with_reentry(cfg, tmp_path):
    eng, db, tg, prices = plan_engine(cfg, tmp_path)
    cfg.raw["trade_style"] = "gunici"
    cfg.raw["gunici"] = dict(GUN)
    db.set("cash", 53000)
    eng.morning()
    buys = [p for p in db.open_pendings() if p["side"] == "BUY"]
    assert len(buys) == 1  # tüm parayla tek hisse
    b = buys[0]
    assert b["qty"] * b["price"] <= 53000 and b["qty"] * b["price"] > 52000
    t1 = db.get(f"t1:{b['symbol']}")
    assert 640 <= (t1 - b["price"]) * b["qty"] <= 760
    assert b["target"] >= t1
    eng.confirm_pending(b["id"], 1)
    assert "LİMİT SATIŞ" in tg.edits[-1][1]
    # 1. hedef geldi -> SATMA, kârı kilitle
    prices[b["symbol"]] = t1 + 0.05
    eng.intraday()
    assert not [p for p in db.open_pendings() if p["side"] == "SELL"]
    pos = db.position(b["symbol"])
    assert pos["stop"] > b["price"]  # kâr kilitli
    assert any("1. hedefi gördü" in m for m, *_ in tg.sent)
    # fiyat geri döndü -> kârla SAT
    prices[b["symbol"]] = pos["stop"] - 0.05
    eng.intraday()
    sell = [p for p in db.open_pendings() if p["side"] == "SELL"][0]
    assert "Kâr kilidi" in sell["reason"]
    eng.confirm_pending(sell["id"], 2)
    assert db.position(b["symbol"]) is None and eng.cash() > 53000
    # bir sonraki kontrolde yeni hisse önerilir (farklı bir hisse)
    eng.intraday()
    new = [p for p in db.open_pendings() if p["side"] == "BUY"]
    assert len(new) == 1 and new[0]["symbol"] != b["symbol"]


def test_text_messages(cfg, tmp_path):
    import main as m
    eng, db, tg, prices = plan_engine(cfg, tmp_path, names=["ALARK", "GARAN"])
    cfg.raw["trade_style"] = "gunici"
    cfg.raw["gunici"] = dict(GUN)
    real = Telegram("", "1")
    m.register_commands(real, eng, cfg)
    p0 = prices["ALARK"]
    r = real.text_handler("Alark aldım 53 binlik satış kac")
    assert db.position("ALARK")["qty"] == int(53000 // p0) and "Zarar kes" in r
    assert "hedef" in real.text_handler("alark")
    r = real.text_handler(f"ALARK sattım {p0 * 1.02:.2f}".replace(".", ","))
    assert db.position("ALARK") is None and "K/Z" in r
    assert "Anlayamadım" in real.text_handler("merhaba")


def test_daytrade_runs_to_stretch_target(cfg, tmp_path):
    eng, db, tg, prices = plan_engine(cfg, tmp_path)
    cfg.raw["trade_style"] = "gunici"
    cfg.raw["gunici"] = dict(GUN, stretch_max_pct=3.0, stretch_range_share=10, lock_share=0.65, trail_after_t1_pct=0.6)
    eng.manual_buy("UP0", 500, 100.0)
    pos = db.position("UP0")
    assert pos["target"] == pytest.approx(103.0)
    for px in (101.4, 102.2, 103.1):  # yükselmeye devam
        prices["UP0"] = px
        eng.intraday()
    sell = [p for p in db.open_pendings() if p["side"] == "SELL"]
    assert sell and "HEDEF" in sell[0]["reason"]
    assert (sell[0]["price"] - 100) * 500 > 1400  # ~%3 -> 1.500 TL


def test_fill_corrections(cfg, tmp_path):
    import main as m
    eng, db, tg, prices = plan_engine(cfg, tmp_path, names=["YKBNK", "GARAN"])
    cfg.raw["trade_style"] = "gunici"
    cfg.raw["gunici"] = dict(GUN)
    db.set("cash", 48000)
    eng.morning()
    pid = db.open_pendings()[0]["id"]
    eng.confirm_pending(pid, 1)  # butona basıldı
    real = Telegram("", "1")
    m.register_commands(real, eng, cfg)
    p0 = db.position("YKBNK")["entry_price"]
    r = real.text_handler(f"{p0 * 0.99:.2f} den aldim".replace(".", ","))
    assert "düzeltildi" in r and db.position("YKBNK")["entry_price"] == pytest.approx(round(p0 * 0.99, 2))
    r = real.text_handler("Ykbnk 48 bin 1259 hisse")
    pos = db.position("YKBNK")
    assert pos["qty"] == 1259 and pos["entry_price"] == pytest.approx(round(p0 * 0.99, 2))
    assert pos["stop"] < pos["entry_price"] < db.get("t1:YKBNK") <= pos["target"]
