"""
价格监控状态机：阈值穿越（below/above）+ 瞬时暴跌 + 持续崩盘。

正确性增强（相对原版）：
- 迟滞带（hysteresis）：触发后必须价格回到 level*(1±h%) 才解除，避免抖动反复触发。
- 历史剪枝：按窗口最大值的 2 倍主动 popleft，避免 O(N) 全扫。
- cooldown：每个告警 key 独立计时。
- 全部从 ``config.get()`` 拿快照，与配置写入完全解耦（无锁竞争）。
"""
from __future__ import annotations

import logging
import time
from typing import Deque, Optional, Tuple

import config
import notifier
import state

log = logging.getLogger("tix.monitor")


def _find_old_price(
    history: Deque[Tuple[float, float]], cutoff_ts: float
) -> Optional[float]:
    """找 ``ts <= cutoff_ts`` 的最新一个价格点；history 按 ts 升序。"""
    best: Optional[float] = None
    for ts, price in history:
        if ts > cutoff_ts:
            break
        best = price
    return best


def _emit(symbol: str, kind: str, title: str, message: str, price: float) -> None:
    notifier.dispatch(title, message)
    state.store.push_alert(
        state.AlertRecord(
            ts=time.time(),
            symbol=symbol,
            kind=kind,
            title=title,
            message=message,
            price=price,
        )
    )
    log.info("ALERT %s/%s: %s @ %s", symbol, kind, title, price)


def process_price(symbol: str, price: float) -> None:
    cfg = config.get()
    coin_cfg = cfg.coins.get(symbol)
    if coin_cfg is None or not coin_cfg.enabled:
        return

    cs = state.store.ensure(symbol)
    now = time.time()

    cs.history.append((now, price))
    cs.last_price = price
    cs.last_update = now

    # 主动剪枝：保留 max(window) * 2 长度的历史，防止 deque 长期堆积。
    keep = max(coin_cfg.flash_crash.window_sec, coin_cfg.slow_crash.window_sec) * 2
    while cs.history and now - cs.history[0][0] > keep:
        cs.history.popleft()

    cooldown = coin_cfg.cooldown
    hyst = coin_cfg.hysteresis_pct / 100.0

    # ---- 跌破阈值 ----
    for level in coin_cfg.price_below_list:
        key = f"below_{level}"
        was = cs.triggered.get(key, False)
        if not was and price <= level:
            if now - cs.last_alert_time.get(key, 0) >= cooldown:
                _emit(
                    symbol,
                    "below",
                    f"🚨 {symbol.upper()} 跌破 {level}",
                    f"当前价格: {price}",
                    price,
                )
                cs.triggered[key] = True
                cs.last_alert_time[key] = now
        elif was and price >= level * (1 + hyst):
            cs.triggered[key] = False

    # ---- 突破阈值 ----
    for level in coin_cfg.price_above_list:
        key = f"above_{level}"
        was = cs.triggered.get(key, False)
        if not was and price >= level:
            if now - cs.last_alert_time.get(key, 0) >= cooldown:
                _emit(
                    symbol,
                    "above",
                    f"🚀 {symbol.upper()} 突破 {level}",
                    f"当前价格: {price}",
                    price,
                )
                cs.triggered[key] = True
                cs.last_alert_time[key] = now
        elif was and price <= level * (1 - hyst):
            cs.triggered[key] = False

    # ---- 瞬时暴跌 / 持续崩盘 ----
    for kind, rule, emoji, label in (
        ("flash_crash", coin_cfg.flash_crash, "💥", "瞬时暴跌"),
        ("slow_crash", coin_cfg.slow_crash, "☠️", "持续崩盘"),
    ):
        old = _find_old_price(cs.history, now - rule.window_sec)
        if old is None or old <= 0:
            continue
        pct = (price - old) / old * 100
        was = cs.triggered.get(kind, False)
        if not was and pct <= rule.drop_pct:
            if now - cs.last_alert_time.get(kind, 0) >= cooldown:
                _emit(
                    symbol,
                    kind,
                    f"{emoji} {symbol.upper()} {label}",
                    f"{rule.window_sec}s 跌幅: {pct:.2f}%\n当前价格: {price}",
                    price,
                )
                cs.triggered[kind] = True
                cs.last_alert_time[kind] = now
        # 解除条件：跌幅恢复到阈值以上 + 迟滞带
        elif was and pct > rule.drop_pct + abs(rule.drop_pct) * hyst:
            cs.triggered[kind] = False
