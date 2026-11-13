"""Teknik analiz: uzmanların grafikte baktığı şeylerin kod hali.

İçerik
  * Destek / direnç   : son tepe-dip noktalarından (swing) kümelenmiş fiyat seviyeleri
  * Trend çizgisi     : son diplerden geçen yükselen destek çizgisi (ve kırılımı)
  * Fibonacci         : son yükseliş dalgasının %38,2 / %50 / %61,8 geri çekilme seviyeleri
  * Bollinger bantları: daralma (sıkışma) ve üst bant kırılımı
  * ADX               : trendin gücü (25 üstü güçlü trend)
  * Stokastik RSI     : kısa vadeli aşırı alım / aşırı satım ve dönüş
  * OBV               : hacim fiyatı onaylıyor mu
  * Ichimoku          : fiyat bulutun üstünde mi, Tenkan/Kijun kesişimi
  * VWAP              : gün içi ortalama maliyetin üstünde mi (gün içi veri varsa)
  * Mum formasyonları : yutan boğa/ayı, çekiç, kayan yıldız, sabah yıldızı, doji

`analyze(df)` hepsini hesaplar ve 0..1 arası bir teknik puan + Türkçe notlar döndürür.
Bu araçlar geleceği garanti etmez; birlikte bakıldığında ihtimali iyileştirmeye yarar.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .indicators import ema, rsi


# ------------------------------------------------------------------ göstergeler
def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std()
    return mid + k * sd, mid, mid - k * sd


def adx(df: pd.DataFrame, n: int = 14):
    h, l, c = df["High"], df["Low"], df["Close"]
    up, down = h.diff(), -l.diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    pdi = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    mdi = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = (100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)).fillna(0)
    return dx.ewm(alpha=1 / n, adjust=False).mean(), pdi, mdi


def stoch_rsi(close: pd.Series, n: int = 14, k: int = 3, d: int = 3):
    r = rsi(close, n)
    lo, hi = r.rolling(n).min(), r.rolling(n).max()
    st = ((r - lo) / (hi - lo).replace(0, np.nan)).fillna(0.5) * 100
    kk = st.rolling(k).mean()
    return kk, kk.rolling(d).mean()


def obv(df: pd.DataFrame) -> pd.Series:
    sign = np.sign(df["Close"].diff()).fillna(0)
    return (sign * df["Volume"]).cumsum()


def ichimoku(df: pd.DataFrame):
    h, l = df["High"], df["Low"]
    tenkan = (h.rolling(9).max() + l.rolling(9).min()) / 2
    kijun = (h.rolling(26).max() + l.rolling(26).min()) / 2
    span_a = ((tenkan + kijun) / 2).shift(26)
    span_b = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    return tenkan, kijun, span_a, span_b


def vwap_today(df: pd.DataFrame) -> float | None:
    """Gün içi mumlar varsa bugünün VWAP'ı."""
    if df is None or df.empty or not hasattr(df.index, "date"):
        return None
    last_day = df.index[-1].date()
    d = df[[i.date() == last_day for i in df.index]]
    if d.empty or d["Volume"].sum() <= 0:
        return None
    tp = (d["High"] + d["Low"] + d["Close"]) / 3
    return float((tp * d["Volume"]).sum() / d["Volume"].sum())


# ------------------------------------------------------------------ yapı (destek / direnç / trend)
def swing_points(df: pd.DataFrame, k: int = 3):
    """Tepe ve dip noktaları: k mum öncesi ve sonrasından yüksek/alçak olanlar."""
    h, l = df["High"].values, df["Low"].values
    highs, lows = [], []
    for i in range(k, len(df) - k):
        if h[i] == max(h[i - k:i + k + 1]):
            highs.append((i, h[i]))
        if l[i] == min(l[i - k:i + k + 1]):
            lows.append((i, l[i]))
    return highs, lows


def sr_levels(df: pd.DataFrame, k: int = 3, tol_pct: float = 0.6, lookback: int = 150) -> list[tuple[float, int]]:
    """Kümelenmiş destek/direnç seviyeleri: (seviye, kaç kez test edildi)."""
    d = df.iloc[-lookback:]
    highs, lows = swing_points(d, k)
    pts = sorted([p for _, p in highs] + [p for _, p in lows])
    levels: list[list[float]] = []
    for p in pts:
        if levels and abs(p / np.mean(levels[-1]) - 1) * 100 <= tol_pct:
            levels[-1].append(p)
        else:
            levels.append([p])
    return [(float(np.mean(g)), len(g)) for g in levels]


def nearest_levels(price: float, levels: list[tuple[float, int]]):
    sup = [lv for lv in levels if lv[0] < price * 0.999]
    res = [lv for lv in levels if lv[0] > price * 1.001]
    s = max(sup, key=lambda x: x[0]) if sup else None
    r = min(res, key=lambda x: x[0]) if res else None
    return s, r


def trendline(df: pd.DataFrame, k: int = 3, lookback: int = 120):
    """Son iki yükselen dipten geçen destek çizgisi. (eğim/mum, bugünkü değer, başlangıç_index) ya da None"""
    d = df.iloc[-lookback:]
    _, lows = swing_points(d, k)
    if len(lows) < 2:
        return None
    (i1, p1), (i2, p2) = lows[-2], lows[-1]
    if p2 <= p1 or i2 == i1:
        return None
    slope = (p2 - p1) / (i2 - i1)
    now_val = p2 + slope * (len(d) - 1 - i2)
    return slope, float(now_val), len(df) - len(d) + i1, p1


def fib_levels(df: pd.DataFrame, lookback: int = 60) -> dict | None:
    d = df.iloc[-lookback:]
    lo_i, hi_i = int(d["Low"].values.argmin()), int(d["High"].values.argmax())
    lo, hi = float(d["Low"].iloc[lo_i]), float(d["High"].iloc[hi_i])
    if hi_i <= lo_i or hi <= lo:  # son dalga yükseliş değil
        return None
    span = hi - lo
    return {"0": hi, "23.6": hi - 0.236 * span, "38.2": hi - 0.382 * span, "50": hi - 0.5 * span,
            "61.8": hi - 0.618 * span, "100": lo}


def candle_patterns(df: pd.DataFrame) -> list[tuple[str, int]]:
    """Son mumlardaki formasyonlar: (ad, +1 boğa / -1 ayı / 0 kararsız)."""
    if len(df) < 3:
        return []
    o, h, l, c = (df[x].values for x in ("Open", "High", "Low", "Close"))
    out = []
    body = abs(c[-1] - o[-1])
    rng = max(h[-1] - l[-1], 1e-9)
    lower = min(o[-1], c[-1]) - l[-1]
    upper = h[-1] - max(o[-1], c[-1])
    prev_body = abs(c[-2] - o[-2])
    if c[-2] < o[-2] and c[-1] > o[-1] and c[-1] >= o[-2] and o[-1] <= c[-2] and body > prev_body:
        out.append(("Yutan boğa mumu", 1))
    if c[-2] > o[-2] and c[-1] < o[-1] and o[-1] >= c[-2] and c[-1] <= o[-2] and body > prev_body:
        out.append(("Yutan ayı mumu", -1))
    if lower >= 2 * body and upper <= body * 0.6 and body / rng < 0.4:
        out.append(("Çekiç", 1))
    if upper >= 2 * body and lower <= body * 0.6 and body / rng < 0.4:
        out.append(("Kayan yıldız", -1))
    if body / rng < 0.1:
        out.append(("Doji (kararsızlık)", 0))
    b3 = abs(c[-3] - o[-3])
    if c[-3] < o[-3] and abs(c[-2] - o[-2]) < b3 * 0.4 and c[-1] > o[-1] and c[-1] > (o[-3] + c[-3]) / 2:
        out.append(("Sabah yıldızı", 1))
    return out


# ------------------------------------------------------------------ toplu analiz
@dataclass
class TA:
    score: float                      # 0..1
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    support: float | None = None
    resistance: float | None = None
    support_hits: int = 0
    resistance_hits: int = 0
    trend_line: float | None = None
    fib: dict | None = None
    levels: list = field(default_factory=list)
    adx: float = 0.0
    details: dict = field(default_factory=dict)


def analyze(df: pd.DataFrame, intraday: pd.DataFrame | None = None) -> TA | None:
    """df: ana zaman dilimi (BIST: günlük, kripto: saatlik). intraday: varsa 15 dk'lık mumlar."""
    if df is None or len(df) < 60:
        return None
    c = df["Close"]
    price = float(c.iloc[-1])
    pts: list[float] = []
    notes, warns = [], []

    # Destek / direnç (kısa vadede gün içi veri daha hassas)
    base = intraday if intraday is not None and len(intraday) >= 60 else df
    levels = sr_levels(base) + sr_levels(df, tol_pct=1.0)
    px_now = float(base["Close"].iloc[-1])
    s, r = nearest_levels(px_now, levels)
    if s:
        dist = (px_now / s[0] - 1) * 100
        if dist <= 1.5:
            pts.append(1.0)
            notes.append(f"Desteğe yakın ({s[0]:.2f}, {s[1]} kez test edildi)")
        else:
            pts.append(0.6)
    if r:
        room = (r[0] / px_now - 1) * 100
        if room < 0.8:
            pts.append(0.1)
            warns.append(f"Hemen üstünde direnç var ({r[0]:.2f})")
        else:
            pts.append(0.8)
    else:
        pts.append(1.0)
        notes.append("Üstünde yakın direnç yok (zirve bölgesi)")

    # Trend çizgisi
    tl = trendline(df)
    if tl:
        _, val, _, _ = tl
        if price >= val:
            pts.append(0.9)
            notes.append("Yükselen trend çizgisinin üstünde")
        else:
            pts.append(0.1)
            warns.append("Trend çizgisi aşağı kırıldı")
    # Fibonacci
    fib = fib_levels(df)
    if fib:
        if fib["61.8"] <= price <= fib["38.2"]:
            pts.append(0.9)
            notes.append("Fibonacci %38–%62 geri çekilme bölgesinde (alım bölgesi)")
        elif price > fib["23.6"]:
            pts.append(0.6)
        elif price < fib["61.8"]:
            pts.append(0.2)
            warns.append("Fibonacci %61,8 altına sarktı")

    # Bollinger
    up, mid, lo = bollinger(c)
    width = (up - lo) / mid
    if width.iloc[-1] <= width.iloc[-120:].quantile(0.2) and price > mid.iloc[-1]:
        pts.append(0.9)
        notes.append("Bollinger sıkışması, yukarı patlama ihtimali")
    elif price > up.iloc[-1]:
        pts.append(0.6)
        notes.append("Bollinger üst bandını kırdı")
    elif price < mid.iloc[-1]:
        pts.append(0.3)

    # ADX
    a, pdi, mdi = adx(df)
    av = float(a.iloc[-1])
    if av >= 25 and pdi.iloc[-1] > mdi.iloc[-1]:
        pts.append(1.0)
        notes.append(f"ADX {av:.0f}: güçlü yükseliş trendi")
    elif av >= 25:
        pts.append(0.0)
        warns.append(f"ADX {av:.0f}: güçlü DÜŞÜŞ trendi")
    else:
        pts.append(0.5)

    # Stokastik RSI
    k, d = stoch_rsi(c)
    kv, dv, kp, dp = k.iloc[-1], d.iloc[-1], k.iloc[-2], d.iloc[-2]
    if kp <= dp and kv > dv and kv < 50:
        pts.append(1.0)
        notes.append("Stokastik RSI dipten yukarı döndü")
    elif kv > 85:
        pts.append(0.3)
        warns.append("Stokastik RSI aşırı alımda")
    else:
        pts.append(0.6 if kv > dv else 0.4)

    # OBV
    ob = obv(df)
    ob_up = ob.iloc[-1] > ob.iloc[-10]
    pr_up = c.iloc[-1] > c.iloc[-10]
    if ob_up and pr_up:
        pts.append(0.9)
        notes.append("Hacim (OBV) yükselişi onaylıyor")
    elif pr_up and not ob_up:
        pts.append(0.3)
        warns.append("Fiyat çıkıyor ama hacim onaylamıyor")
    else:
        pts.append(0.5)

    # Ichimoku
    tenkan, kijun, sa, sb = ichimoku(df)
    cloud_top = np.nanmax([sa.iloc[-1], sb.iloc[-1]]) if not (np.isnan(sa.iloc[-1]) and np.isnan(sb.iloc[-1])) else None
    if cloud_top is not None:
        if price > cloud_top and tenkan.iloc[-1] > kijun.iloc[-1]:
            pts.append(1.0)
            notes.append("Ichimoku: bulutun üstünde, Tenkan > Kijun")
        elif price < np.nanmin([sa.iloc[-1], sb.iloc[-1]]):
            pts.append(0.1)
            warns.append("Ichimoku: bulutun altında")
        else:
            pts.append(0.5)

    # VWAP (gün içi)
    vw = vwap_today(intraday) if intraday is not None else None
    if vw:
        ip = float(intraday["Close"].iloc[-1])
        if ip >= vw:
            pts.append(0.8)
            notes.append(f"Gün içi VWAP üstünde ({vw:.2f})")
        else:
            pts.append(0.3)
            warns.append(f"Gün içi VWAP altında ({vw:.2f})")

    # Mum formasyonları (gün içi varsa onda, yoksa ana grafikte)
    for name, sign in candle_patterns(intraday if intraday is not None and len(intraday) > 5 else df):
        if sign > 0:
            pts.append(1.0)
            notes.append(f"Mum formasyonu: {name}")
        elif sign < 0:
            pts.append(0.0)
            warns.append(f"Mum formasyonu: {name}")

    score = float(np.mean(pts)) if pts else 0.5
    return TA(score=score, notes=notes, warnings=warns,
              support=s[0] if s else None, resistance=r[0] if r else None,
              support_hits=s[1] if s else 0, resistance_hits=r[1] if r else 0,
              trend_line=tl[1] if tl else None, fib=fib, levels=levels, adx=av,
              details={"tl": tl, "vwap": vw})
