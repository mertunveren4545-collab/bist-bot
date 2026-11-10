"""Geriye dönük test: stratejiyi geçmiş verilerde dener (haber puanı nötr kabul edilir).

    python backtest.py               -> config.yaml'daki hisselerle son 2 yıl
    python backtest.py --yil 3

Gerçek parayla başlamadan önce mutlaka çalıştır. Geçmiş sonuç geleceği garanti etmez.
"""
from __future__ import annotations

import argparse

import pandas as pd

from bot.config import load_config
from bot.risk import position_plan, round_tick, trail_stop
from bot.strategy import score_symbol


def run(data: dict[str, pd.DataFrame], cfg, start_cash: float, warmup: int = 200) -> dict:
    risk, strat = cfg.risk, cfg.strategy
    th, sell_th = strat["buy_threshold"], strat["sell_threshold"]
    dates = sorted(set().union(*[set(df.index) for df in data.values()]))
    cash, positions, trades, curve = start_cash, {}, [], []
    pending_buys: list[tuple[str, float]] = []  # ertesi gün açılışta alınacaklar

    for di, day in enumerate(dates):
        if di < warmup:
            continue
        # 1) Dünkü sinyalleri bugünün açılışında al
        for sym, atr_val in pending_buys:
            df = data[sym]
            if day not in df.index or sym in positions or len(positions) >= risk["max_open_positions"]:
                continue
            o = float(df.loc[day, "Open"])
            eq = cash + sum(p["qty"] * float(data[s].loc[:day, "Close"].iloc[-1]) for s, p in positions.items())
            plan = position_plan(o, atr_val, eq, cash, risk)
            if plan:
                cash -= plan.qty * o
                positions[sym] = {"qty": plan.qty, "entry_price": o, "stop": plan.stop,
                                  "target": plan.target, "highest": o, "atr": atr_val, "day": di}
        pending_buys = []

        # 2) Gün içi stop / hedef kontrolü
        for sym in list(positions):
            df = data[sym]
            if day not in df.index:
                continue
            p, row = positions[sym], df.loc[day]
            exit_px, why = None, ""
            if row.Low <= p["stop"]:
                exit_px, why = min(p["stop"], float(row.Open)), "stop"
            elif row.High >= p["target"]:
                exit_px, why = max(p["target"], float(row.Open)), "hedef"
            if exit_px is None:
                p["stop"], p["highest"] = trail_stop(p, float(row.Close), p["atr"], risk)
                continue
            cash += p["qty"] * exit_px
            trades.append({"sym": sym, "pnl": (exit_px - p["entry_price"]) * p["qty"],
                           "ret": exit_px / p["entry_price"] - 1, "why": why, "days": di - p["day"]})
            del positions[sym]

        # 3) Gün sonu puanlama -> çıkış ve yeni sinyaller
        cands = []
        for sym, df in data.items():
            hist = df.loc[:day]
            if len(hist) < 60 or hist.index[-1] != day:
                continue
            sc = score_symbol(sym, hist, 0.0, strat)
            if not sc:
                continue
            if sym in positions:
                p = positions[sym]
                held = di - p["day"]
                if sc.total < sell_th or (held >= risk["max_holding_days"] and sc.price <= p["entry_price"]):
                    px = round_tick(sc.price)
                    cash += p["qty"] * px
                    trades.append({"sym": sym, "pnl": (px - p["entry_price"]) * p["qty"],
                                   "ret": px / p["entry_price"] - 1, "why": "puan/süre", "days": held})
                    del positions[sym]
            elif sc.total >= th:
                cands.append(sc)
        cands.sort(key=lambda s: s.total, reverse=True)
        pending_buys = [(s.symbol, s.atr) for s in cands[:risk["max_open_positions"]]]

        eq = cash + sum(p["qty"] * float(data[s].loc[:day, "Close"].iloc[-1]) for s, p in positions.items())
        curve.append((day, eq))

    eq_s = pd.Series([e for _, e in curve], index=[d for d, _ in curve]) if curve else pd.Series(dtype=float)
    dd = ((eq_s / eq_s.cummax()) - 1).min() * 100 if len(eq_s) else 0.0
    wins = [t for t in trades if t["pnl"] > 0]
    return {
        "son_deger": float(eq_s.iloc[-1]) if len(eq_s) else start_cash,
        "getiri_pct": (float(eq_s.iloc[-1]) / start_cash - 1) * 100 if len(eq_s) else 0.0,
        "max_dusus_pct": float(dd),
        "islem": len(trades),
        "kazanma_orani": len(wins) / len(trades) * 100 if trades else 0.0,
        "ort_getiri_pct": sum(t["ret"] for t in trades) / len(trades) * 100 if trades else 0.0,
        "trades": trades,
        "curve": eq_s,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yil", type=int, default=2)
    args = ap.parse_args()
    cfg = load_config()
    from bot.data import daily

    data = {}
    for s in cfg.universe:
        df = daily(s, period=f"{args.yil + 1}y")
        if df is not None and len(df) > 250:
            data[s] = df
    idx = daily(cfg.raw.get("market_index", "XU100"), period=f"{args.yil + 1}y")
    res = run(data, cfg, float(cfg.raw.get("starting_cash", 100000)))
    print(f"\nHisse sayısı : {len(data)}")
    print(f"Son değer    : {res['son_deger']:,.0f} ₺  ({res['getiri_pct']:+.1f}%)")
    if idx is not None and len(res["curve"]):
        i0 = idx["Close"].loc[:res["curve"].index[0]].iloc[-1]
        print(f"XU100 aynı dönem: {(idx['Close'].iloc[-1] / i0 - 1) * 100:+.1f}%")
    print(f"En büyük düşüş: {res['max_dusus_pct']:.1f}%")
    print(f"İşlem sayısı : {res['islem']}  | Kazanma oranı: {res['kazanma_orani']:.0f}%"
          f"  | Ort. işlem getirisi: {res['ort_getiri_pct']:+.2f}%")
    print("\nNot: TL bazlı getiriyi enflasyonla ve XU100 ile karşılaştır.")


if __name__ == "__main__":
    main()
