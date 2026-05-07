"""
SQLite 持久层：连接管理、schema 初始化、CRUD。

设计要点:
- WAL 模式 + ``synchronous=NORMAL``：高并发下读写不互斥，掉电不丢已 commit 数据。
- ``check_same_thread=False`` + 进程级 RLock：FastAPI 异步线程 + 监控写入安全共享一条连接。
- ``isolation_level=None`` + 显式 BEGIN/COMMIT：把事务边界握在自己手里。
- DB 路径默认 ``data/tix.db``，便于挂载到 Docker volume。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

log = logging.getLogger("tix.db")

DB_FILE = Path(os.environ.get("TIX_DB", "data/tix.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS coins (
    symbol            TEXT PRIMARY KEY,
    price_below_list  TEXT NOT NULL DEFAULT '[]',
    price_above_list  TEXT NOT NULL DEFAULT '[]',
    flash_window_sec  INTEGER NOT NULL DEFAULT 60,
    flash_drop_pct    REAL    NOT NULL DEFAULT -2.5,
    slow_window_sec   INTEGER NOT NULL DEFAULT 300,
    slow_drop_pct     REAL    NOT NULL DEFAULT -5.0,
    cooldown          INTEGER NOT NULL DEFAULT 300,
    hysteresis_pct    REAL    NOT NULL DEFAULT 0.5,
    enabled           INTEGER NOT NULL DEFAULT 1,
    created_at        REAL    NOT NULL,
    updated_at        REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    section     TEXT PRIMARY KEY,
    data        TEXT NOT NULL,
    updated_at  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS alerts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    symbol   TEXT NOT NULL,
    kind     TEXT NOT NULL,
    title    TEXT NOT NULL,
    message  TEXT NOT NULL,
    price    REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_ts     ON alerts(ts DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_symbol ON alerts(symbol, ts DESC);
"""

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def init() -> None:
    """初始化 db 连接 + 建表；幂等，可重复调用。"""
    global _conn
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(
        str(DB_FILE),
        check_same_thread=False,
        isolation_level=None,
        timeout=10,
    )
    _conn.row_factory = sqlite3.Row
    with _lock:
        _conn.execute("PRAGMA journal_mode=WAL;")
        _conn.execute("PRAGMA synchronous=NORMAL;")
        _conn.execute("PRAGMA foreign_keys=ON;")
        _conn.executescript(_SCHEMA)
    log.info("db ready: %s", DB_FILE)


def conn() -> sqlite3.Connection:
    if _conn is None:
        init()
    return _conn  # type: ignore[return-value]


@contextmanager
def cursor() -> Iterator[sqlite3.Cursor]:
    """带锁的游标上下文。所有写操作请走这里。"""
    with _lock:
        cur = conn().cursor()
        try:
            yield cur
        finally:
            cur.close()


@contextmanager
def transaction() -> Iterator[sqlite3.Cursor]:
    """显式事务：要么全成功要么全回滚。"""
    with _lock:
        cur = conn().cursor()
        cur.execute("BEGIN")
        try:
            yield cur
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        finally:
            cur.close()


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            try:
                _conn.close()
            finally:
                _conn = None
