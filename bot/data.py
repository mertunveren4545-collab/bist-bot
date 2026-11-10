"""Fiyat verisi: Yahoo Finance (yfinance). BIST verisi ~15 dk gecikmeli gelir.

Daha hızlı veri istersen bu dosyadaki iki fonksiyonu (daily, last_prices)
kendi veri kaynağınla değiştirmen yeterli; botun geri kalanı aynı kalır.
"""
from __future__ import annotations

import logging
import time

import pandas as pd

log = logging.getLogger(__name__)

INDEX_MAP = {"XU100": "XU100.IS", "XU030": "XU030.IS"}


def yahoo_symbol(code: str) -> str:
    code = code.upper()
    if code in INDEX_MAP:
        return INDEX_MAP[code]
    return code if code.endswith(".IS") else f"{code}.IS"


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close"])
    return df


_cache: dict[str, tuple[float, pd.DataFrame]] = {}


def daily(code: str, period: str = "2y", cache_sec: int = 3600) -> pd.DataFrame | None:
    """Günlük mum verisi. Saatlik önbellek kullanır."""
    sym = yahoo_symbol(code)
    hit = _cache.get(sym)
    if hit and time.time() - hit[0] < cache_sec:
        return hit[1]
    try:
        import yfinance as yf

        df = yf.download(sym, period=period, interval="1d", progress=False, auto_adjust=True)
        if df is None or df.empty:
            log.warning("Veri yok: %s", sym)
            return None
        df = _clean(df)
        _cache[sym] = (time.time(), df)
        return df
    except Exception as e:  # ağ hatası vb.
        log.error("Veri hatası %s: %s", sym, e)
        return None


def last_prices(codes: list[str]) -> dict[str, float]:
    """Seans içi son fiyatlar (15 dk'lık mumlardan)."""
    out: dict[str, float] = {}
    if not codes:
        return out
    try:
        import yfinance as yf

        syms = [yahoo_symbol(c) for c in codes]
        df = yf.download(syms, period="2d", interval="15m", progress=False,
                         auto_adjust=True, group_by="ticker")
        for c, s in zip(codes, syms):
            try:
                sub = df[s] if len(syms) > 1 else _clean(df)
                val = sub["Close"].dropna()
                if not val.empty:
                    out[c] = float(val.iloc[-1])
            except Exception:
                continue
    except Exception as e:
        log.error("Anlık fiyat hatası: %s", e)
    # Eksik kalanlar için günlük kapanışı kullan
    for c in codes:
        if c not in out:
            d = daily(c)
            if d is not None and not d.empty:
                out[c] = float(d["Close"].iloc[-1])
    return out
