"""
配置层：Pydantic schema + 原子持久化 + 线程安全访问。

设计要点：
- 任何读取都通过 ``get()``，确保拿到一致的快照。
- 任何写入都通过 ``update()``，先校验再 tmp+rename 原子写盘。
- 解析失败时备份坏文件为 .bad，使用默认配置避免静默丢失数据。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger("tix.config")

CONFIG_FILE = Path(os.environ.get("TIX_CONFIG", "config.json"))


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
        default=0.5,
        ge=0,
        le=50,
        description="阈值迟滞带 (%)，防止抖动重复触发",
    )

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
    # 第一次启动后请通过 TIX_AUTH_PASSWORD 环境变量或修改 config.json 立刻替换
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


DEFAULT = AppConfig(
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
_cached: AppConfig = DEFAULT.model_copy(deep=True)


def _atomic_write(path: Path, payload: str) -> None:
    parent = path.parent if str(path.parent) else Path(".")
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".cfg-", suffix=".tmp", dir=str(parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load() -> AppConfig:
    """从磁盘加载配置；首次启动写入默认；解析失败时备份并回退默认。"""
    global _cached
    with _lock:
        if not CONFIG_FILE.exists():
            _atomic_write(
                CONFIG_FILE,
                json.dumps(DEFAULT.model_dump(), indent=2, ensure_ascii=False),
            )
            _cached = DEFAULT.model_copy(deep=True)
            log.info("config not found, wrote default to %s", CONFIG_FILE)
            return _cached
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            _cached = AppConfig.model_validate(raw)
            log.info("config loaded: %d coins", len(_cached.coins))
        except (json.JSONDecodeError, ValidationError) as e:
            backup = CONFIG_FILE.with_suffix(".bad")
            try:
                CONFIG_FILE.replace(backup)
            except OSError:
                pass
            _atomic_write(
                CONFIG_FILE,
                json.dumps(DEFAULT.model_dump(), indent=2, ensure_ascii=False),
            )
            _cached = DEFAULT.model_copy(deep=True)
            log.error(
                "config invalid (%s); backed up to %s and using DEFAULT",
                e, backup,
            )
        # 环境变量覆盖密码（容器化部署常用）
        env_pwd = os.environ.get("TIX_AUTH_PASSWORD")
        if env_pwd:
            _cached.auth.password = env_pwd
        return _cached


def get() -> AppConfig:
    """获取当前缓存的配置（深拷贝，保证调用方不会污染共享状态）。"""
    with _lock:
        return _cached.model_copy(deep=True)


def update(cfg: AppConfig) -> AppConfig:
    """校验、原子写盘、刷新缓存。"""
    global _cached
    validated = AppConfig.model_validate(cfg.model_dump())
    with _lock:
        _atomic_write(
            CONFIG_FILE,
            json.dumps(validated.model_dump(), indent=2, ensure_ascii=False),
        )
        _cached = validated
        log.info("config updated: %d coins", len(_cached.coins))
        return _cached.model_copy(deep=True)
