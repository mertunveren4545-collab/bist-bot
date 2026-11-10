"""Kağıt üzerinde (sanal) işlem: emir anında, küçük bir kayma (slippage) ile gerçekleşir."""
from __future__ import annotations

from ..risk import round_tick
from .base import Broker, OrderResult


class PaperBroker(Broker):
    name = "paper"

    def __init__(self, slippage_pct: float = 0.1):
        self.slip = slippage_pct / 100

    def buy(self, symbol, qty, price, stop, target, reason) -> OrderResult:
        fill = round_tick(price * (1 + self.slip))
        return OrderResult("FILLED", fill, qty, "Sanal alım gerçekleşti")

    def sell(self, symbol, qty, price, reason) -> OrderResult:
        fill = round_tick(price * (1 - self.slip))
        return OrderResult("FILLED", fill, qty, "Sanal satış gerçekleşti")
