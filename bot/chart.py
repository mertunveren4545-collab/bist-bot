"""Telegram'a gönderilen grafik: mumlar + EMA'lar + destek/direnç + trend çizgisi + Fibonacci
+ alış / 1. hedef / 2. hedef / zarar kes çizgileri."""
from __future__ import annotations

import io

import numpy as np
import pandas as pd

from . import ta as tamod
from .indicators import ema

BG, FG, GRID = "#0f1419", "#d7dde4", "#232b35"
UP, DOWN = "#26a69a", "#ef5350"


def render(symbol: str, df: pd.DataFrame, title_tf: str = "15 dk", bars: int = 90,
           entry: float | None = None, t1: float | None = None, t2: float | None = None,
           stop: float | None = None, note: str = "") -> bytes | None:
    if df is None or len(df) < 30:
        return None
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    full = df.copy()
    d = full.iloc[-bars:]
    x = np.arange(len(d))
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(9, 5.6), dpi=110, sharex=True,
                                  gridspec_kw={"height_ratios": [4, 1]}, facecolor=BG)
    for a in (ax, axv):
        a.set_facecolor(BG)
        a.tick_params(colors=FG, labelsize=7)
        a.grid(color=GRID, linewidth=0.5)
        for sp in a.spines.values():
            sp.set_color(GRID)

    o, h, l, c = d["Open"].values, d["High"].values, d["Low"].values, d["Close"].values
    col = np.where(c >= o, UP, DOWN)
    ax.vlines(x, l, h, color=col, linewidth=0.8)
    ax.bar(x, np.maximum(abs(c - o), (h - l) * 0.02), bottom=np.minimum(o, c), color=col, width=0.6)
    axv.bar(x, d["Volume"].values, color=col, width=0.6, alpha=0.7)

    e20, e50 = ema(full["Close"], 20).iloc[-bars:], ema(full["Close"], 50).iloc[-bars:]
    ax.plot(x, e20.values, color="#f5c542", linewidth=1, label="EMA20")
    ax.plot(x, e50.values, color="#7e57c2", linewidth=1, label="EMA50")
    up, mid, lo = tamod.bollinger(full["Close"])
    ax.fill_between(x, lo.iloc[-bars:].values, up.iloc[-bars:].values, color="#90a4ae", alpha=0.07)

    price = float(c[-1])
    lo_view, hi_view = float(l.min()), float(h.max())
    lv = [(v, n) for v, n in tamod.sr_levels(full) if lo_view * 0.99 <= v <= hi_view * 1.01 and n >= 2]
    below = sorted([z for z in lv if z[0] < price], key=lambda z: -z[0])[:3]
    above = sorted([z for z in lv if z[0] >= price], key=lambda z: z[0])[:3]
    for lvl, hits in below + above:
        isdest = lvl < price
        ax.axhline(lvl, color="#26a69a" if isdest else "#ef9a9a", linestyle="--", linewidth=0.8, alpha=0.7)
        ax.text(x[0], lvl, f" {'Destek' if isdest else 'Direnç'} {lvl:,.2f} ({hits}x)",
                color="#80cbc4" if isdest else "#ef9a9a", fontsize=6.5, va="bottom")

    tl = tamod.trendline(full)
    if tl:
        slope, now_val, i0, p0 = tl
        start = len(full) - bars
        xs = np.array([max(i0, start), len(full) - 1])
        ys = p0 + slope * (xs - i0)
        ax.plot(xs - start, ys, color="#42a5f5", linewidth=1.2, label="Trend çizgisi")

    fib = tamod.fib_levels(full, lookback=min(bars, 60))
    if fib:
        for k in ("38.2", "50", "61.8"):
            ax.axhline(fib[k], color="#ab47bc", linewidth=0.5, alpha=0.5)
            ax.text(x[-1], fib[k], f"Fib {k} ", color="#ce93d8", fontsize=6, ha="right", va="bottom")

    def hl(v, color, label):
        if v:
            ax.axhline(v, color=color, linewidth=1.3)
            ax.text(len(d) + 0.5, v, f"{label} {v:,.2f}", color=color, fontsize=7.5, va="center",
                    fontweight="bold", bbox=dict(facecolor=BG, edgecolor=color, pad=1.5, linewidth=0.6))

    hl(entry, "#ffffff", "Alış")
    hl(t1, "#66bb6a", "1.H")
    hl(t2, "#00e676", "2.H")
    hl(stop, "#ff5252", "Stop")

    ax.set_title(f"{symbol} · {title_tf} · {price:,.2f}" + (f"  —  {note}" if note else ""), color=FG, fontsize=10, loc="left")
    ax.legend(loc="upper left", fontsize=6.5, facecolor=BG, edgecolor=GRID, labelcolor=FG)
    step = max(1, len(d) // 6)
    labels = [i.strftime("%d.%m %H:%M") if hasattr(i, "strftime") else str(i) for i in d.index]
    axv.set_xticks(x[::step])
    axv.set_xticklabels(labels[::step], rotation=0)
    ax.set_xlim(-1, len(d) + 16)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    return buf.getvalue()
