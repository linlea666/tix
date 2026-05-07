"""
运行时状态：每币的价格历史、告警状态、连接状态、告警历史持久化。

线程安全：所有变更都通过内部 RLock；snapshot() 返回的是冷拷贝。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Dict, List, Tuple

log = logging.getLogger("tix.state")

STATE_FILE = Path(os.environ.get("TIX_STATE", "state.json"))

# 价格历史最长保留点数；2048 在 1 秒一 tick 下 ~34 分钟，足够覆盖 5 分钟 slow window。
DEFAULT_HISTORY_MAXLEN = 2048
DEFAULT_ALERTS_MAXLEN = 200


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
    def __init__(
        self,
        history_maxlen: int = DEFAULT_HISTORY_MAXLEN,
        alerts_maxlen: int = DEFAULT_ALERTS_MAXLEN,
    ) -> None:
        self._lock = threading.RLock()
        self._coins: Dict[str, CoinState] = {}
        self._alerts: Deque[AlertRecord] = deque(maxlen=alerts_maxlen)
        self._connected: Dict[str, bool] = {}
        self._history_maxlen = history_maxlen

    # -------- coin state --------
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

    # -------- connection --------
    def set_connected(self, symbol: str, ok: bool) -> None:
        with self._lock:
            self._connected[symbol] = ok

    def is_connected(self, symbol: str) -> bool:
        with self._lock:
            return self._connected.get(symbol, False)

    # -------- alerts --------
    def push_alert(self, rec: AlertRecord) -> None:
        with self._lock:
            self._alerts.appendleft(rec)

    def alerts(self) -> List[AlertRecord]:
        with self._lock:
            return list(self._alerts)

    # -------- snapshot for UI / API --------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "coins": {
                    s: {
                        "last_price": c.last_price,
                        "last_update": c.last_update,
                        "history_len": len(c.history),
                        "connected": self._connected.get(s, False),
                        "triggered": dict(c.triggered),
                    }
                    for s, c in self._coins.items()
                },
                "alerts": [a.to_dict() for a in self._alerts],
            }

    # -------- persistence --------
    def persist(self) -> None:
        """落盘告警历史；价格历史是高频数据，不持久化。"""
        with self._lock:
            try:
                payload = {"alerts": [a.to_dict() for a in self._alerts]}
                tmp = STATE_FILE.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                os.replace(tmp, STATE_FILE)
            except OSError as e:
                log.warning("persist state failed: %s", e)

    def restore(self) -> None:
        with self._lock:
            if not STATE_FILE.exists():
                return
            try:
                raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                for a in reversed(raw.get("alerts", [])):
                    self._alerts.appendleft(AlertRecord(**a))
                log.info("restored %d alert records", len(self._alerts))
            except (json.JSONDecodeError, OSError, TypeError) as e:
                log.warning("restore state failed: %s", e)


store = StateStore()
