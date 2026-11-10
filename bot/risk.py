"""Risk yönetimi: lot hesabı, stop/hedef, iz süren stop, günlük limitler."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Plan:
    qty: int
    stop: float
    target: float
    cost: float


def tick(price: float) -> float:
    """BIST fiyat adımı (pay piyasası)."""
    if price < 20:
        return 0.01
    if price < 50:
        return 0.02
    if price < 100:
        return 0.05
    if price < 250:
        return 0.10
    if price < 500:
        return 0.25
    if price < 1000:
        return 0.50
    if price < 2500:
        return 1.00
    return 2.50


def round_tick(price: float) -> float:
    t = tick(price)
    return round(round(price / t) * t, 2)


def position_plan(price: float, atr_val: float, equity: float, cash: float, risk: dict) -> Plan | None:
    """ATR'ye göre lot: stop olursa sermayenin risk_per_trade_pct'i kadar kaybedilir."""
    if price <= 0 or atr_val <= 0:
        return None
    stop = round_tick(price - risk["stop_loss_atr"] * atr_val)
    target = round_tick(price + risk["take_profit_atr"] * atr_val)
    per_share_risk = price - stop
    if per_share_risk <= 0:
        return None
    risk_money = equity * risk["risk_per_trade_pct"] / 100
    qty_risk = math.floor(risk_money / per_share_risk)
    qty_cap = math.floor(equity * risk["max_position_pct"] / 100 / price)
    usable_cash = cash - equity * risk["min_cash_pct"] / 100
    qty_cash = math.floor(max(0.0, usable_cash) / price)
    qty = min(qty_risk, qty_cap, qty_cash)
    if qty < 1:
        return None
    return Plan(qty=qty, stop=stop, target=target, cost=round(qty * price, 2))


def trail_stop(pos: dict, price: float, atr_val: float, risk: dict) -> tuple[float, float]:
    """Fiyat yeni zirve yaptıkça stop'u yukarı taşı. (yeni_stop, yeni_zirve) döner."""
    highest = max(pos["highest"], price)
    new_stop = round_tick(highest - risk["trailing_stop_atr"] * atr_val)
    return max(pos["stop"], new_stop), highest


def exit_reason(pos: dict, price: float, score: float | None, days_held: int, risk: dict,
                sell_threshold: float) -> str | None:
    if price <= pos["stop"]:
        return "STOP (zarar kes / iz süren stop)"
    if price >= pos["target"]:
        return "HEDEF (kâr al)"
    if score is not None and score < sell_threshold:
        return f"Puan düştü ({score:.0f})"
    if days_held >= risk["max_holding_days"] and price <= pos["entry_price"]:
        return f"{days_held} gündür kâra geçmedi"
    return None
