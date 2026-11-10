"""Midas yarı otomatik mod.

Midas'ın bireysel yatırımcıya açık bir emir API'si olmadığı için bot emri
kendisi vermez. Bunun yerine:
  1. Sinyali Telegram'a "✅ Midas'ta aldım / ❌ Geç" butonlarıyla gönderir.
  2. Sen Midas uygulamasında emri girersin (bot lotu ve fiyatı hazır yazar).
  3. ✅'ye basınca bot pozisyonu kaydeder; stop/hedef takibini o yapar,
     satış zamanı gelince yine Telegram'dan haber verir.
Gerçek dolum fiyatın farklıysa:  /fiyat <no> <fiyat>  komutuyla düzeltebilirsin.
"""
from __future__ import annotations

from .base import Broker, OrderResult


class MidasManualBroker(Broker):
    name = "midas"

    def __init__(self, storage):
        self.db = storage

    def buy(self, symbol, qty, price, stop, target, reason) -> OrderResult:
        pid = self.db.add_pending(symbol, "BUY", qty, price, stop, target, reason)
        return OrderResult("PENDING", price, qty, "Midas onayı bekleniyor", pending_id=pid)

    def sell(self, symbol, qty, price, reason) -> OrderResult:
        pid = self.db.add_pending(symbol, "SELL", qty, price, None, None, reason)
        return OrderResult("PENDING", price, qty, "Midas onayı bekleniyor", pending_id=pid)
