"""
配置层：Pydantic schema 校验 + SQLite 持久化 + 内存缓存。

存储拆分:
- 表 ``coins``：每币一行（便于增删改查、可视化）
- 表 ``settings``：``pushover/telegram/webhook/auth`` 各 1 行 JSON

调用方一律用 ``get()`` 读快照（深拷贝，无锁污染），用 ``update()`` 写。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List

from pydantic import BaseModel, Field, ValidationError, field_validator

import db

log = logging.getLogger("tix.config")


class CrashRule(BaseModel):
    window_sec: int = Field(gt=0, le=24 * 3600, description="时间窗口(秒)")
    drop_pct: float = Field(lt=0, description="跌幅%（必须为负）")


class CoinConfig(BaseModel):
    price_below_list: List[float] = Field(default_factory=list)
    price_above_list: List[float] = Field(default_factory=list)
    flash_crash: CrashRule = CrashRule(window_sec=60, drop_pct=-2.5)
    slow_crash: CrashRule = CrashRule(window_sec=300, drop_pct=-5.0)
    cooldown: int = Field(default=300, ge=0, description="冷却时间(秒)")
    hysteresis_pct: float = Field(
        default=0.5, ge=0, le=50,
        description="阈值迟滞带 (%)，防止抖动重复触发",
    )
    enabled: bool = Field(default=True, description="是否启用监控")

    @field_validator("price_below_list", "price_above_list")
    @classmethod
    def _positive(cls, v: List[float]) -> List[float]:
        return sorted({float(x) for x in v if float(x) > 0})


class PushoverConfig(BaseModel):
    enabled: bool = False
    token: str = ""
    user: str = ""
    priority: int = 2
    sound: str = "siren"


class TelegramConfig(BaseModel):
    enabled: bool = False
    bot_token: str = ""
    chat_id: str = ""


class WebhookConfig(BaseModel):
    enabled: bool = False
    url: str = ""


class WebAuthConfig(BaseModel):
    username: str = "admin"
    password: str = "changeme"


class AppConfig(BaseModel):
    coins: Dict[str, CoinConfig] = Field(default_factory=dict)
    pushover: PushoverConfig = PushoverConfig()
    telegram: TelegramConfig = TelegramConfig()
    webhook: WebhookConfig = WebhookConfig()
    auth: WebAuthConfig = WebAuthConfig()

    @field_validator("coins")
    @classmethod
    def _normalize_symbols(
        cls, v: Dict[str, CoinConfig]
    ) -> Dict[str, CoinConfig]:
        return {k.lower().strip(): val for k, val in v.items() if k.strip()}


_DEFAULT_SEED = AppConfig(
    coins={
        "btcusdt": CoinConfig(
            price_below_list=[80000, 75000],
            price_above_list=[90000, 95000],
            flash_crash=CrashRule(window_sec=60, drop_pct=-2.5),
            slow_crash=CrashRule(window_sec=300, drop_pct=-5.0),
        ),
        "ethusdt": CoinConfig(
            price_below_list=[3000],
            price_above_list=[4000],
            flash_crash=CrashRule(window_sec=60, drop_pct=-3.0),
            slow_crash=CrashRule(window_sec=300, drop_pct=-6.0),
        ),
    }
)

_lock = threading.RLock()
_cached: AppConfig = _DEFAULT_SEED.model_copy(deep=True)


# ---------------------------- helpers ----------------------------


def _row_to_coin(r) -> CoinConfig:
    return CoinConfig(
        price_below_list=json.loads(r["price_below_list"]),
        price_above_list=json.loads(r["price_above_list"]),
        flash_crash=CrashRule(
            window_sec=int(r["flash_window_sec"]),
            drop_pct=float(r["flash_drop_pct"]),
        ),
        slow_crash=CrashRule(
            window_sec=int(r["slow_window_sec"]),
            drop_pct=float(r["slow_drop_pct"]),
        ),
        cooldown=int(r["cooldown"]),
        hysteresis_pct=float(r["hysteresis_pct"]),
        enabled=bool(r["enabled"]),
    )


def _read_db() -> AppConfig:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM coins ORDER BY symbol")
        coins = {r["symbol"]: _row_to_coin(r) for r in cur.fetchall()}
        cur.execute("SELECT section, data FROM settings")
        sections: Dict[str, Any] = {
            r["section"]: json.loads(r["data"]) for r in cur.fetchall()
        }
    return AppConfig(
        coins=coins,
        pushover=PushoverConfig(**sections.get("pushover", {})),
        telegram=TelegramConfig(**sections.get("telegram", {})),
        webhook=WebhookConfig(**sections.get("webhook", {})),
        auth=WebAuthConfig(**sections.get("auth", {})),
    )


def _write_db(cfg: AppConfig) -> None:
    """把整个 AppConfig 全量同步到 SQLite（事务原子）。"""
    now = time.time()
    with db.transaction() as cur:
        cur.execute("SELECT symbol FROM coins")
        existing = {r["symbol"] for r in cur.fetchall()}
        new = set(cfg.coins.keys())

        for sym in existing - new:
            cur.execute("DELETE FROM coins WHERE symbol=?", (sym,))

        for sym, c in cfg.coins.items():
            row = (
                json.dumps(c.price_below_list),
                json.dumps(c.price_above_list),
                c.flash_crash.window_sec,
                c.flash_crash.drop_pct,
                c.slow_crash.window_sec,
                c.slow_crash.drop_pct,
                c.cooldown,
                c.hysteresis_pct,
                1 if c.enabled else 0,
                now,
            )
            if sym in existing:
                cur.execute(
                    """
                    UPDATE coins SET
                        price_below_list=?, price_above_list=?,
                        flash_window_sec=?, flash_drop_pct=?,
                        slow_window_sec=?,  slow_drop_pct=?,
                        cooldown=?, hysteresis_pct=?,
                        enabled=?, updated_at=?
                    WHERE symbol=?
                    """,
                    row + (sym,),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO coins
                        (price_below_list, price_above_list,
                         flash_window_sec, flash_drop_pct,
                         slow_window_sec,  slow_drop_pct,
                         cooldown, hysteresis_pct, enabled,
                         updated_at, symbol, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    row + (sym, now),
                )

        for section, data in (
            ("pushover", cfg.pushover.model_dump()),
            ("telegram", cfg.telegram.model_dump()),
            ("webhook", cfg.webhook.model_dump()),
            ("auth", cfg.auth.model_dump()),
        ):
            cur.execute(
                """
                INSERT INTO settings (section, data, updated_at)
                VALUES (?,?,?)
                ON CONFLICT(section) DO UPDATE SET
                    data       = excluded.data,
                    updated_at = excluded.updated_at
                """,
                (section, json.dumps(data, ensure_ascii=False), now),
            )


def _seed_if_empty() -> bool:
    """空库时写入默认种子。返回是否实际写入。"""
    with db.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM coins")
        coin_n = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM settings")
        setting_n = cur.fetchone()["n"]
    if coin_n == 0 and setting_n == 0:
        _write_db(_DEFAULT_SEED)
        log.info("seeded default config (2 coins)")
        return True
    return False


# ---------------------------- public API ----------------------------


def load() -> AppConfig:
    """从 SQLite 加载到内存缓存。空库时写入默认种子。"""
    global _cached
    db.init()
    with _lock:
        _seed_if_empty()
        _cached = _read_db()

        # 环境变量优先：仅运行时覆盖密码，不写回 db（避免容器更新泄漏）
        env_pwd = os.environ.get("TIX_AUTH_PASSWORD")
        if env_pwd:
            _cached.auth.password = env_pwd

        log.info("config loaded: %d coins", len(_cached.coins))
        return _cached.model_copy(deep=True)


def get() -> AppConfig:
    with _lock:
        return _cached.model_copy(deep=True)


def update(cfg: AppConfig) -> AppConfig:
    """校验 + 原子写入 + 刷新缓存。"""
    global _cached
    validated = AppConfig.model_validate(cfg.model_dump())
    with _lock:
        _write_db(validated)
        _cached = validated
        log.info("config updated: %d coins", len(_cached.coins))
        return _cached.model_copy(deep=True)


def update_password(new_password: str) -> None:
    """单独修改 admin 密码（不动其他配置）。"""
    new_password = (new_password or "").strip()
    if not new_password:
        raise ValueError("password must not be empty")
    if len(new_password) < 4:
        raise ValueError("password too short (min 4 chars)")
    cfg = get()
    cfg.auth.password = new_password
    update(cfg)
