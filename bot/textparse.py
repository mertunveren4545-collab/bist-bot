"""Telegram'a normal yazıyla yazılan mesajları anlar.

Örnekler:
    "ALARK aldım 53 bin"            -> alım, tutar 53.000
    "Alark aldım 53 binlik"         -> alım, tutar 53.000
    "thyao 100 lot aldım 285,50"    -> alım, 100 lot, fiyat 285,50
    "ALARK sattım 107,3"            -> satış, fiyat 107,30
    "garan sattım"                  -> satış, son fiyattan
"""
from __future__ import annotations

import re

BUY_WORDS = ("aldım", "aldim", "aldık", "aldik", "girdim", "alındı", "alindi")
SELL_WORDS = ("sattım", "sattim", "sattık", "sattik", "çıktım", "ciktim", "çıkdım", "satıldı", "satildi")
STOP = {"ALDIM", "ALDİM", "SATTIM", "SATTİM", "LOT", "ADET", "BIN", "BİN", "BINLIK", "BİNLİK", "FIYAT",
        "FİYAT", "TL", "TANE", "HISSE", "HİSSE", "DAN", "DEN", "TAN", "TEN", "ALDIK", "SATTIK", "GIRDIM",
        "GİRDİM", "ÇIKTIM", "VE", "ILE", "İLE", "BUGÜN", "BUGUN", "SABAH", "LIRA", "LİRA"}


def _num(s: str) -> float:
    s = s.strip()
    if re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", s):  # 53.000 veya 1.234,5
        s = s.replace(".", "")
    return float(s.replace(",", "."))


def parse(text: str, universe: list[str]) -> dict:
    low = text.lower()
    action = None
    if any(w in low for w in SELL_WORDS):
        action = "SELL"
    elif any(w in low for w in BUY_WORDS):
        action = "BUY"
    out = {"action": action, "symbol": None, "qty": None, "amount": None, "price": None}

    up = text.replace("i", "I").replace("ı", "I").upper()  # Türkçe büyük harf sorunlarını önle
    words = re.findall(r"[A-ZÇĞİÖŞÜ]{3,6}", up)
    uni = set(universe)
    for w in words:
        if w in uni:
            out["symbol"] = w
            break
    if not out["symbol"]:
        for w in words:
            if w not in STOP and re.fullmatch(r"[A-Z]{4,5}", w):
                out["symbol"] = w
                break

    # sayılar + arkasındaki birim
    for m in re.finditer(r"(\d[\d.,]*)\s*([a-zçğıöşüA-ZÇĞİÖŞÜ₺]*)", text):
        raw, unit = m.group(1).rstrip(".,"), m.group(2).lower()
        try:
            val = _num(raw)
        except ValueError:
            continue
        before = text[max(0, m.start() - 8):m.start()].lower()
        if unit.startswith(("lot", "adet", "tane")):
            out["qty"] = int(val)
        elif unit.startswith(("bin", "k")):
            out["amount"] = val * 1000
        elif unit.startswith(("tl", "lira", "₺")) and val >= 1000:
            out["amount"] = val
        elif "@" in before or "fiyat" in before or "," in raw or unit.startswith(("dan", "den", "tan", "ten")):
            out["price"] = val
        elif val >= 5000 and out["amount"] is None:
            out["amount"] = val
        elif out["price"] is None:
            out["price"] = val
    return out
