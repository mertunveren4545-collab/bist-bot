"""Haber toplama + Türkçe kelime tabanlı duygu (olumlu/olumsuz) skoru.

Kaynaklar:
  * Google Haberler RSS: her hisse için "<KOD> hisse" araması (KAP haberleri de buradan düşer)
  * Genel ekonomi RSS'leri (config.yaml -> news.general_feeds)

Skor: her haber -1 ile +1 arası. Hisse skoru = son X saatteki haberlerin
yeniliğe göre ağırlıklı ortalaması.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Mozilla/5.0 (BIST-Bot haber okuyucu)"}

# Kelime -> ağırlık. Kelime kökü olarak aranır (ör. "kâr" -> "kârı", "kârını").
POSITIVE = {
    "rekor": 0.6, "kâr art": 0.8, "kar art": 0.8, "net kâr": 0.4, "net kar": 0.4,
    "yükseliş": 0.4, "yükseldi": 0.4, "yukarı yönlü": 0.4, "tavan": 0.6,
    "geri alım": 0.7, "pay geri alım": 0.8, "bedelsiz": 0.8, "temettü": 0.6, "kâr payı": 0.6,
    "ihale kazan": 0.8, "sözleşme imzala": 0.6, "anlaşma imzala": 0.6, "sipariş": 0.5,
    "ihracat": 0.3, "yatırım": 0.2, "kapasite art": 0.5, "büyüme": 0.4, "güçlü": 0.3,
    "hedef fiyat yükselt": 0.8, "al tavsiye": 0.7, "endeks dahil": 0.5, "spk onay": 0.4,
    "faiz indirim": 0.5, "not artır": 0.7, "beklenti üzeri": 0.7, "beklentilerin üzerinde": 0.7,
}
NEGATIVE = {
    "zarar": -0.7, "net zarar": -0.8, "düşüş": -0.4, "düştü": -0.4, "geriledi": -0.4,
    "taban": -0.6, "sert satış": -0.6, "kayıp": -0.4, "ceza": -0.7, "soruşturma": -0.8,
    "dava": -0.5, "iflas": -1.0, "konkordato": -1.0, "haciz": -0.8, "tedbir": -0.7,
    "işlem yasağı": -0.9, "brüt takas": -0.7, "açığa satış yasağı": -0.3, "sermaye artırımı": -0.3,
    "bedelli": -0.4, "hedef fiyat düşür": -0.8, "sat tavsiye": -0.7, "not indir": -0.7,
    "beklenti altı": -0.7, "beklentilerin altında": -0.7, "grev": -0.5, "yangın": -0.6,
    "faiz artır": -0.4, "endeksten çıkar": -0.5, "gözaltı": -0.9,
}

COMPANY_NAMES = {  # aramayı güçlendirmek için şirket adları
    "THYAO": "Türk Hava Yolları", "ASELS": "Aselsan", "TUPRS": "Tüpraş", "EREGL": "Ereğli Demir Çelik",
    "BIMAS": "BİM", "KCHOL": "Koç Holding", "SAHOL": "Sabancı Holding", "GARAN": "Garanti BBVA",
    "AKBNK": "Akbank", "YKBNK": "Yapı Kredi", "ISCTR": "İş Bankası", "TCELL": "Turkcell",
    "TTKOM": "Türk Telekom", "FROTO": "Ford Otosan", "TOASO": "Tofaş", "PGSUS": "Pegasus",
    "SISE": "Şişecam", "SASA": "Sasa Polyester", "PETKM": "Petkim", "MGROS": "Migros",
    "TAVHL": "TAV Havalimanları", "ENKAI": "Enka İnşaat", "ULKER": "Ülker", "CCOLA": "Coca-Cola İçecek",
    "KRDMD": "Kardemir", "EKGYO": "Emlak Konut", "ALARK": "Alarko", "GUBRF": "Gübretaş", "ASTOR": "Astor Enerji",
}


@dataclass
class NewsItem:
    title: str
    link: str
    published: datetime
    source: str
    score: float = 0.0


def score_text(text: str) -> float:
    t = text.lower()
    total, hits = 0.0, 0
    for lex in (POSITIVE, NEGATIVE):
        for word, w in lex.items():
            if word in t:
                total += w
                hits += 1
    if not hits:
        return 0.0
    return max(-1.0, min(1.0, total / max(1, hits) * min(hits, 3) / 1.5))


def _parse_date(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.now(timezone.utc)


def parse_rss(xml_text: str, source: str = "") -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        desc = re.sub("<[^>]+>", " ", it.findtext("description") or "")
        n = NewsItem(
            title=title,
            link=(it.findtext("link") or "").strip(),
            published=_parse_date(it.findtext("pubDate")),
            source=source,
        )
        n.score = score_text(title + " " + desc)
        items.append(n)
    return items


def fetch_feed(url: str, source: str = "", timeout: int = 10) -> list[NewsItem]:
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout)
        r.raise_for_status()
        return parse_rss(r.text, source or url)
    except Exception as e:
        log.warning("RSS okunamadı %s: %s", url, e)
        return []


def google_news_url(query: str) -> str:
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=tr&gl=TR&ceid=TR:tr"


def mentions(item: NewsItem, code: str) -> bool:
    t = item.title.upper()
    if re.search(rf"\b{code}\b", t):
        return True
    name = COMPANY_NAMES.get(code)
    return bool(name and name.upper() in t)


class NewsEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lookback = timedelta(hours=cfg.get("lookback_hours", 48))
        self._general: list[NewsItem] = []

    def refresh_general(self) -> list[NewsItem]:
        items: list[NewsItem] = []
        for url in self.cfg.get("general_feeds", []):
            items.extend(fetch_feed(url))
        self._general = items
        return items

    def for_ticker(self, code: str) -> list[NewsItem]:
        items: list[NewsItem] = []
        if self.cfg.get("google_news_per_ticker", True):
            q = f'"{code}" hisse OR "{COMPANY_NAMES.get(code, code)}"'
            items.extend(fetch_feed(google_news_url(q), "Google Haberler"))
        items.extend(i for i in self._general if mentions(i, code))
        cutoff = datetime.now(timezone.utc) - self.lookback
        seen, out = set(), []
        for i in sorted(items, key=lambda x: x.published, reverse=True):
            key = i.title[:80].lower()
            if i.published >= cutoff and key not in seen:
                seen.add(key)
                out.append(i)
        return out

    @staticmethod
    def aggregate(items: list[NewsItem]) -> float:
        """Yeni haberler daha ağır basar. Sonuç -1..+1"""
        if not items:
            return 0.0
        now = datetime.now(timezone.utc)
        num = den = 0.0
        for i in items:
            age_h = max(0.0, (now - i.published).total_seconds() / 3600)
            w = 1.0 / (1.0 + age_h / 12)
            num += i.score * w
            den += w
        return max(-1.0, min(1.0, num / den)) if den else 0.0

    def market_mood(self) -> float:
        return self.aggregate(self._general)
