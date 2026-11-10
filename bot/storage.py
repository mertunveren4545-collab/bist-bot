"""SQLite veritabanı: pozisyonlar, işlemler, sinyaller, onay bekleyen emirler, ayarlar."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .config import TZ

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    qty INTEGER NOT NULL,
    entry_price REAL NOT NULL,
    stop REAL NOT NULL,
    target REAL NOT NULL,
    highest REAL NOT NULL,
    opened_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,           -- BUY / SELL
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    reason TEXT,
    pnl REAL DEFAULT 0,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty INTEGER NOT NULL,
    price REAL NOT NULL,
    stop REAL,
    target REAL,
    reason TEXT,
    status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING / DONE / REJECTED / EXPIRED
    created_at TEXT NOT NULL,
    message_id INTEGER
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    score REAL NOT NULL,
    details TEXT,
    ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def now() -> datetime:
    return datetime.now(TZ)


def now_s() -> str:
    return now().isoformat(timespec="seconds")


class Storage:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---------- genel ----------
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    # ---------- anahtar/değer ----------
    def get(self, key: str, default=None):
        rows = self._all("SELECT v FROM kv WHERE k=?", (key,))
        return json.loads(rows[0]["v"]) if rows else default

    def set(self, key: str, value) -> None:
        self._exec("INSERT OR REPLACE INTO kv(k, v) VALUES(?, ?)", (key, json.dumps(value)))

    # ---------- nakit ----------
    def cash(self, starting: float) -> float:
        c = self.get("cash")
        if c is None:
            self.set("cash", starting)
            return float(starting)
        return float(c)

    def add_cash(self, amount: float, starting: float) -> float:
        c = self.cash(starting) + amount
        self.set("cash", round(c, 2))
        return c

    # ---------- pozisyonlar ----------
    def positions(self) -> list[dict]:
        return self._all("SELECT * FROM positions ORDER BY opened_at")

    def position(self, symbol: str) -> dict | None:
        rows = self._all("SELECT * FROM positions WHERE symbol=?", (symbol,))
        return rows[0] if rows else None

    def open_position(self, symbol, qty, price, stop, target) -> None:
        existing = self.position(symbol)
        if existing:  # ortalama maliyetle ekle
            total = existing["qty"] + qty
            avg = (existing["qty"] * existing["entry_price"] + qty * price) / total
            self._exec(
                "UPDATE positions SET qty=?, entry_price=?, stop=?, target=? WHERE symbol=?",
                (total, round(avg, 4), stop, target, symbol),
            )
        else:
            self._exec(
                "INSERT INTO positions VALUES(?,?,?,?,?,?,?)",
                (symbol, qty, price, stop, target, price, now_s()),
            )

    def update_stop(self, symbol, stop, highest) -> None:
        self._exec("UPDATE positions SET stop=?, highest=? WHERE symbol=?", (stop, highest, symbol))

    def close_position(self, symbol) -> None:
        self._exec("DELETE FROM positions WHERE symbol=?", (symbol,))

    # ---------- işlemler ----------
    def add_trade(self, symbol, side, qty, price, reason="", pnl=0.0) -> None:
        self._exec(
            "INSERT INTO trades(symbol, side, qty, price, reason, pnl, ts) VALUES(?,?,?,?,?,?,?)",
            (symbol, side, qty, price, reason, round(pnl, 2), now_s()),
        )

    def trades_today(self) -> list[dict]:
        today = now().date().isoformat()
        return self._all("SELECT * FROM trades WHERE ts LIKE ? ORDER BY id", (f"{today}%",))

    def all_trades(self) -> list[dict]:
        return self._all("SELECT * FROM trades ORDER BY id")

    # ---------- onay bekleyen ----------
    def add_pending(self, symbol, side, qty, price, stop, target, reason) -> int:
        cur = self._exec(
            "INSERT INTO pending(symbol, side, qty, price, stop, target, reason, created_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (symbol, side, qty, price, stop, target, reason, now_s()),
        )
        return int(cur.lastrowid)

    def pending(self, pid: int) -> dict | None:
        rows = self._all("SELECT * FROM pending WHERE id=?", (pid,))
        return rows[0] if rows else None

    def open_pendings(self) -> list[dict]:
        return self._all("SELECT * FROM pending WHERE status='PENDING' ORDER BY id")

    def set_pending_status(self, pid: int, status: str) -> None:
        self._exec("UPDATE pending SET status=? WHERE id=?", (status, pid))

    def set_pending_message(self, pid: int, message_id: int) -> None:
        self._exec("UPDATE pending SET message_id=? WHERE id=?", (message_id, pid))

    # ---------- sinyaller ----------
    def add_signal(self, symbol, score, details: dict) -> None:
        self._exec(
            "INSERT INTO signals(symbol, score, details, ts) VALUES(?,?,?,?)",
            (symbol, score, json.dumps(details, ensure_ascii=False), now_s()),
        )
