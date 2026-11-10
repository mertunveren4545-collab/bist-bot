"""Tüm aracı kurum bağlantılarının uyması gereken ortak arayüz."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OrderResult:
    status: str            # FILLED / PENDING / REJECTED
    price: float = 0.0
    qty: int = 0
    message: str = ""
    pending_id: int | None = None


class Broker:
    name = "base"

    def buy(self, symbol: str, qty: int, price: float, stop: float, target: float,
            reason: str) -> OrderResult:
        raise NotImplementedError

    def sell(self, symbol: str, qty: int, price: float, reason: str) -> OrderResult:
        raise NotImplementedError
