"""Kripto fiyat verisi (TL pariteleri), gecikmesiz.

Kaynak: Binance'in herkese açık piyasa verisi (data-api.binance.vision). Sadece fiyat okunur,
hesap/anahtar gerekmez. Midas Kripto fiyatı birkaç binde fark edebilir; işlemi Midas'taki
fiyattan yaparsın, bot hedefleri o fiyata göre yeniden hesaplar.
"""
from __future__ import annotations

import logging
import time

import pandas as pd
import requests

log = logging.getLogger(__name__)

HOSTS = ["https://data-api.binance.vision", "https://api.binance.com", "https://api1.binance.com"]
_cache: dict[str, tuple[float, pd.DataFrame]] = {}


def pair(code: str) -> str:
    code = code.upper()
    return code if code.endswith("TRY") else f"{code}TRY"


def _get(path: str, params: dict):
    last = None
    for h in HOSTS:
        try:
            r = requests.get(h + path, params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            last = f"{h}: HTTP {r.status_code}"
        except Exception as e:  # sonraki adresi dene
            last = f"{h}: {e}"
    log.warning("Kripto verisi alınamadı %s %s (%s)", path, params.get("symbol"), last)
    return None


def candles(code: str, interval: str = "1h", limit: int = 300, cache_sec: int = 120) -> pd.DataFrame | None:
    """Saatlik mumlar. Strateji puanlaması bu tabloyu kullanır (günlük yerine saatlik)."""
    key = f"{pair(code)}:{interval}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < cache_sec:
        return hit[1]
    data = _get("/api/v3/klines", {"symbol": pair(code), "interval": interval, "limit": limit})
    if not data:
        return None
    df = pd.DataFrame(data, columns=["t", "Open", "High", "Low", "Close", "Volume", "ct", "qv", "n", "tb", "tq", "i"])
    df.index = pd.to_datetime(df["t"], unit="ms", utc=True)
    df = df[["Open", "High", "Low", "Close", "Volume"]].astype(float)
    _cache[key] = (time.time(), df)
    return df


def last_prices(codes: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for c in codes:
        d = _get("/api/v3/ticker/price", {"symbol": pair(c)})
        if d and "price" in d:
            out[c] = float(d["price"])
    return out
