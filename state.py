"""
运行时状态：
- 价格历史（内存 deque，高频，不持久化）
- 连接状态（内存 dict）
- 告警历史（SQLite ``alerts`` 表，进程重启保留）

线程安全：所有变更通过 RLock；snapshot 返回冷拷贝。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

import db

log = logging.getLogger("tix.state")

DEFAULT_HISTORY_MAXLEN = 2048
DEFAULT_ALERTS_VIEW = 200          # 仪表板 / API 默认展示条数
ALERTS_DB_KEEP = DEFAULT_ALERTS_VIEW * 5  # DB 中长期保留条数


@dataclass
class CoinState:
    history: Deque[Tuple[float, float]] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_HISTORY_MAXLEN)
    )
    last_price: float = 0.0
    last_update: float = 0.0
    triggered: Dict[str, bool] = field(default_factory=dict)
    last_alert_time: Dict[str, float] = field(default_factory=dict)


@dataclass
class AlertRecord:
    ts: float
    symbol: str
    kind: str
    title: str
    message: str
    price: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "symbol": self.symbol,
            "kind": self.kind,
            "title": self.title,
            "message": self.message,
            "price": self.price,
        }


class StateStore:
    def __init__(self, history_maxlen: int = DEFAULT_HISTORY_MAXLEN) -> None:
        self._lock = threading.RLock()
        self._coins: Dict[str, CoinState] = {}
        self._connected: Dict[str, bool] = {}
        self._history_maxlen = history_maxlen

    # -------- coin runtime --------
    def ensure(self, symbol: str) -> CoinState:
        with self._lock:
            cs = self._coins.get(symbol)
            if cs is None:
                cs = CoinState(history=deque(maxlen=self._history_maxlen))
                self._coins[symbol] = cs
            return cs

    def drop(self, symbol: str) -> None:
        with self._lock:
            self._coins.pop(symbol, None)
            self._connected.pop(symbol, None)

    def symbols(self) -> List[str]:
        with self._lock:
            return list(self._coins.keys())

    def set_connected(self, symbol: str, ok: bool) -> None:
        with self._lock:
            self._connected[symbol] = ok

    def is_connected(self, symbol: str) -> bool:
        with self._lock:
            return self._connected.get(symbol, False)

    # -------- alerts (SQLite) --------
    def push_alert(self, rec: AlertRecord) -> None:
        with db.cursor() as cur:
            cur.execute(
                "INSERT INTO alerts (ts, symbol, kind, title, message, price)"
                " VALUES (?,?,?,?,?,?)",
                (rec.ts, rec.symbol, rec.kind, rec.title, rec.message, rec.price),
            )
            # 简单的容量保留：超出按 ts 升序删除老记录
            cur.execute("SELECT COUNT(*) AS n FROM alerts")
            n = cur.fetchone()["n"]
            if n > ALERTS_DB_KEEP:
                cur.execute(
                    "DELETE FROM alerts WHERE id IN ("
                    " SELECT id FROM alerts ORDER BY ts ASC LIMIT ?)",
                    (n - ALERTS_DB_KEEP,),
                )

    def alerts(
        self,
        limit: int = DEFAULT_ALERTS_VIEW,
        symbol: Optional[str] = None,
    ) -> List[AlertRecord]:
        with db.cursor() as cur:
            if symbol:
                cur.execute(
                    "SELECT ts,symbol,kind,title,message,price FROM alerts"
                    " WHERE symbol=? ORDER BY ts DESC LIMIT ?",
                    (symbol, limit),
                )
            else:
                cur.execute(
                    "SELECT ts,symbol,kind,title,message,price FROM alerts"
                    " ORDER BY ts DESC LIMIT ?",
                    (limit,),
                )
            return [AlertRecord(**dict(row)) for row in cur.fetchall()]

    def clear_alerts(self) -> int:
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM alerts")
            n = cur.fetchone()["n"]
            cur.execute("DELETE FROM alerts")
        return int(n)

    # -------- snapshot --------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            coins = {
                s: {
                    "last_price": c.last_price,
                    "last_update": c.last_update,
                    "history_len": len(c.history),
                    "connected": self._connected.get(s, False),
                    "triggered": dict(c.triggered),
                }
                for s, c in self._coins.items()
            }
        alerts = [a.to_dict() for a in self.alerts(limit=DEFAULT_ALERTS_VIEW)]
        return {"coins": coins, "alerts": alerts}


store = StateStore()
