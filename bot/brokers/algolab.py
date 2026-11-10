"""Deniz Yatırım Algolab API bağlantısı (tam otomatik emir) — ŞABLON.

Deniz Yatırım, API dokümanını her müşteriye özel olarak veriyor; uç noktalar
(endpoint), şifreleme ve SMS doğrulama adımları o dokümana göre doldurulmalı.
Bu yüzden aşağıdaki fonksiyonlar bilerek boş bırakıldı: doküman gelmeden
tahminle emir göndermek gerçek parayı riske atar.

Yapılacaklar (doküman gelince):
  1. login(): API anahtarı + kullanıcı adı + şifre ile giriş, SMS kodu doğrulama,
     oturum token'ını saklama.
  2. _send_order(): hisse kodu, yön (alış/satış), lot, fiyat, emir tipi (limit) ile emir.
  3. Emrin gerçekleştiğini kontrol edip OrderResult("FILLED", ...) döndürme.

O zamana kadar bu mod seçilirse bot güvenli tarafta kalır ve emir göndermez.
"""
from __future__ import annotations

import logging

from .base import Broker, OrderResult

log = logging.getLogger(__name__)


class AlgolabBroker(Broker):
    name = "algolab"

    def __init__(self, api_key: str, username: str, password: str):
        self.api_key, self.username, self.password = api_key, username, password
        self.token: str | None = None

    def login(self) -> bool:
        if not (self.api_key and self.username and self.password):
            log.error("Algolab bilgileri .env dosyasında eksik.")
            return False
        # TODO: Deniz Yatırım'ın sana verdiği API dokümanına göre doldur.
        log.error("Algolab girişi henüz yazılmadı (API dokümanı gerekli).")
        return False

    def _send_order(self, symbol: str, side: str, qty: int, price: float) -> OrderResult:
        # TODO: doküman gelince gerçek emir isteği buraya.
        return OrderResult("REJECTED", message="Algolab bağlantısı henüz tamamlanmadı")

    def buy(self, symbol, qty, price, stop, target, reason) -> OrderResult:
        if not self.token and not self.login():
            return OrderResult("REJECTED", message="Algolab girişi yapılamadı")
        return self._send_order(symbol, "BUY", qty, price)

    def sell(self, symbol, qty, price, reason) -> OrderResult:
        if not self.token and not self.login():
            return OrderResult("REJECTED", message="Algolab girişi yapılamadı")
        return self._send_order(symbol, "SELL", qty, price)
