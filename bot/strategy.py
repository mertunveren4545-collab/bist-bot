"""Puanlama stratejisi: Trend + Momentum + Hacim + Haber -> 0..100 puan.

Mantık (sadece ALIM yönlü, BIST'te açığa satış yapılmaz):
  * Trend     : fiyat > EMA20 > EMA50 > EMA200 ne kadar sağlanıyorsa o kadar puan
  * Momentum  : RSI 50-70 bandı ideal, MACD histogramı pozitif ve artıyorsa puan
  * Hacim     : bugünkü hacim / 20 günlük ortalama
  * Haber     : son 48 saatin haber duygu skoru (-1..+1) -> 0..ağırlık
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import ta as tamod
from .indicators import add_all


@dataclass
class Score:
    symbol: str
    total: float
    price: float
    atr: float
    parts: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    ta: object = None

    def summary(self) -> str:
        p = self.parts
        txt = (f"Trend {p.get('trend', 0):.0f} | Momentum {p.get('momentum', 0):.0f} | "
               f"Hacim {p.get('volume', 0):.0f}")
        if "teknik" in p:
            txt += f" | Teknik {p['teknik']:.0f}"
        if p.get("news"):
            txt += f" | Haber {p['news']:.0f}"
        return txt


def _clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score_symbol(symbol: str, df: pd.DataFrame, news_score: float, cfg: dict,
                 intraday: pd.DataFrame | None = None) -> Score | None:
    if df is None or len(df) < 60:
        return None
    d = add_all(df)
    last, prev = d.iloc[-1], d.iloc[-2]
    w = cfg["weights"]
    notes: list[str] = []

    # --- Trend ---
    conds = [
        last.Close > last.EMA20,
        last.EMA20 > last.EMA50,
        last.EMA50 > last.EMA200 if not pd.isna(last.EMA200) else False,
        last.Close > last.EMA200 if not pd.isna(last.EMA200) else False,
    ]
    trend = sum(conds) / len(conds)
    if last.Close >= last.HIGH20 * 0.995:
        trend = min(1.0, trend + 0.15)
        notes.append("Son 20 mumun zirvesine yakın/kırılım")
    if conds[1] and not (prev.EMA20 > prev.EMA50):
        notes.append("EMA20 EMA50'yi yukarı kesti")

    # --- Momentum ---
    r = last.RSI
    if 50 <= r <= cfg.get("rsi_overbought", 75):
        rsi_part = 1.0 - abs(r - 62) / 25
    elif r > cfg.get("rsi_overbought", 75):
        rsi_part = 0.2
        notes.append(f"RSI aşırı alımda ({r:.0f})")
    else:
        rsi_part = _clip01((r - 30) / 40) * 0.5
    macd_part = 0.0
    if last.MACD_HIST > 0:
        macd_part += 0.6
    if last.MACD_HIST > prev.MACD_HIST:
        macd_part += 0.4
    if last.MACD_HIST > 0 and prev.MACD_HIST <= 0:
        notes.append("MACD al sinyali")
    momentum = _clip01(0.5 * _clip01(rsi_part) + 0.5 * macd_part)

    # --- Hacim ---
    vol_ratio = last.Volume / last.VOL_MA20 if last.VOL_MA20 else 1.0
    volume = _clip01((vol_ratio - 0.7) / 1.3)  # 0.7x -> 0, 2x -> 1
    if vol_ratio >= 1.8:
        notes.append(f"Hacim ortalamanın {vol_ratio:.1f} katı")
    if last.Close < prev.Close:  # düşüşte gelen hacim iyi değil
        volume *= 0.4

    # --- Haber ---
    news = _clip01((news_score + 1) / 2)
    if news_score >= 0.3:
        notes.append("Olumlu haber akışı")
    elif news_score <= -0.3:
        notes.append("OLUMSUZ haber akışı")

    parts = {
        "trend": trend * w["trend"],
        "momentum": momentum * w["momentum"],
        "volume": volume * w["volume"],
        "news": news * w.get("news", 0),
    }
    t = None
    if w.get("teknik"):
        t = tamod.analyze(df, intraday)
        parts["teknik"] = (t.score if t else 0.5) * w["teknik"]
        if t:
            notes.extend(t.notes[:3])
            notes.extend("⚠️ " + x for x in t.warnings[:2])
    total = sum(parts.values())
    # Çok olumsuz haber varsa teknik ne olursa olsun alma
    if news_score <= -0.5:
        total = min(total, cfg["buy_threshold"] - 1)
        notes.append("Ağır olumsuz haber: alım engellendi")

    return Score(symbol, round(total, 1), float(last.Close), float(last.ATR), parts, notes, ta=t)


def market_ok(index_df: pd.DataFrame | None) -> bool:
    """Endeks 50 günlük ortalamanın üstündeyse piyasa 'alıma uygun'."""
    if index_df is None or len(index_df) < 60:
        return True  # veri yoksa engelleme
    d = add_all(index_df)
    return bool(d.Close.iloc[-1] > d.EMA50.iloc[-1])
