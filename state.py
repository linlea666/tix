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


@dataclass
class ActiveAlert:
    key: str
    symbol: str
    kind: str
    level: Optional[float]
    title: str
    message: str
    price: float
    first_at: float
    last_notify_at: float
    notify_count: int
    ack: bool
    ack_at: Optional[float]
    pushover_receipt: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "symbol": self.symbol,
            "kind": self.kind,
            "level": self.level,
            "title": self.title,
            "message": self.message,
            "price": self.price,
            "first_at": self.first_at,
            "last_notify_at": self.last_notify_at,
            "notify_count": self.notify_count,
            "ack": self.ack,
            "ack_at": self.ack_at,
            "pushover_receipt": self.pushover_receipt,
        }


def make_alert_key(symbol: str, kind: str, level: Optional[float] = None) -> str:
    """规范化的 active_alerts 主键。"""
    if level is None:
        return f"{symbol}:{kind}"
    # 用 g 格式避免 80000.0 / 80000 不一致
    return f"{symbol}:{kind}:{level:g}"


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

    # -------- active alerts (Firing / Ack / Resolved 三态) --------
    @staticmethod
    def _row_to_active(row) -> ActiveAlert:
        # row 可能没有 pushover_receipt 列（极旧 DB 未迁移），用 try 取值
        try:
            receipt = row["pushover_receipt"]
        except (IndexError, KeyError):
            receipt = None
        return ActiveAlert(
            key=row["key"],
            symbol=row["symbol"],
            kind=row["kind"],
            level=row["level"],
            title=row["title"],
            message=row["message"],
            price=row["price"],
            first_at=row["first_at"],
            last_notify_at=row["last_notify_at"],
            notify_count=int(row["notify_count"]),
            ack=bool(row["ack"]),
            ack_at=row["ack_at"],
            pushover_receipt=receipt,
        )

    def upsert_active(
        self,
        key: str,
        symbol: str,
        kind: str,
        level: Optional[float],
        title: str,
        message: str,
        price: float,
    ) -> tuple[bool, ActiveAlert]:
        """Upsert active alert。返回 (是否新建, 当前记录)。
        - 新建时立即返回（调用方负责发首条推送）
        - 已存在时只更新 message/price/title（保持 first_at/notify_count 不变）
        """
        now = time.time()
        with db.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM active_alerts WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                cur.execute(
                    """
                    INSERT INTO active_alerts (
                        key, symbol, kind, level,
                        title, message, price,
                        first_at, last_notify_at, notify_count, ack, ack_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (key, symbol, kind, level, title, message, price,
                     now, now, 1, 0, None),
                )
                row = cur.execute(
                    "SELECT * FROM active_alerts WHERE key=?", (key,)
                ).fetchone()
                return True, self._row_to_active(row)
            else:
                cur.execute(
                    """
                    UPDATE active_alerts
                       SET title=?, message=?, price=?
                     WHERE key=?
                    """,
                    (title, message, price, key),
                )
                row = cur.execute(
                    "SELECT * FROM active_alerts WHERE key=?", (key,)
                ).fetchone()
                return False, self._row_to_active(row)

    def list_active(
        self,
        only_unacked: bool = False,
        symbol: Optional[str] = None,
    ) -> List[ActiveAlert]:
        sql = "SELECT * FROM active_alerts"
        clauses, params = [], []
        if only_unacked:
            clauses.append("ack=0")
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY first_at ASC"
        with db.cursor() as cur:
            return [self._row_to_active(r) for r in cur.execute(sql, params)]

    def ack_active(self, key: str) -> bool:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE active_alerts SET ack=1, ack_at=? WHERE key=? AND ack=0",
                (time.time(), key),
            )
            return cur.rowcount > 0

    def ack_all_active(self) -> int:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE active_alerts SET ack=1, ack_at=? WHERE ack=0",
                (time.time(),),
            )
            return int(cur.rowcount)

    def resolve_active(self, key: str) -> Optional[ActiveAlert]:
        """删除一条 active 记录，返回被删除的副本（用于发"已解除"消息）。"""
        with db.cursor() as cur:
            row = cur.execute(
                "SELECT * FROM active_alerts WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            rec = self._row_to_active(row)
            cur.execute("DELETE FROM active_alerts WHERE key=?", (key,))
            return rec

    def drop_active_by_symbol(self, symbol: str) -> int:
        with db.cursor() as cur:
            cur.execute(
                "DELETE FROM active_alerts WHERE symbol=?", (symbol,)
            )
            return int(cur.rowcount)

    def update_active_notify(
        self,
        key: str,
        ts: float,
        receipt: Optional[str] = None,
    ) -> None:
        """重发后调用：更新 last_notify_at + count，并刷新 receipt（若有）。"""
        with db.cursor() as cur:
            if receipt is None:
                cur.execute(
                    "UPDATE active_alerts "
                    "  SET last_notify_at=?, notify_count=notify_count+1 "
                    "WHERE key=?",
                    (ts, key),
                )
            else:
                cur.execute(
                    "UPDATE active_alerts "
                    "  SET last_notify_at=?, notify_count=notify_count+1, "
                    "      pushover_receipt=?, ack=0, ack_at=NULL "
                    "WHERE key=?",
                    (ts, receipt, key),
                )

    def set_active_receipt(self, key: str, receipt: Optional[str]) -> None:
        with db.cursor() as cur:
            cur.execute(
                "UPDATE active_alerts SET pushover_receipt=? WHERE key=?",
                (receipt, key),
            )

    def mark_active_acked(
        self, key: str, ts: Optional[float] = None
    ) -> bool:
        """由 Pushover receipt 查询发现已 ack 时调用。返回是否实际改变状态。"""
        with db.cursor() as cur:
            cur.execute(
                "UPDATE active_alerts SET ack=1, ack_at=? WHERE key=? AND ack=0",
                (ts or time.time(), key),
            )
            return cur.rowcount > 0

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
