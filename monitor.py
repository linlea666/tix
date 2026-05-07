"""
价格监控状态机：阈值穿越 / 瞬时暴跌 / 持续崩盘。

Firing / Resolve 模型:
- 触发条件首次成立 → upsert active_alerts + 立即推送一次 (priority=2)
- 条件持续成立 → 由 reminder 协程按 repeat_interval_sec 重发
- Pushover 客户端 ack → reminder 检测到后停止重发（保留 active 记录）
- 价格回到迟滞带外 → 删除 active 记录（可选发"已解除"通知）
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
    best: Optional[float] = None
    for ts, price in history:
        if ts > cutoff_ts:
            break
        best = price
    return best


def _arm(
    symbol: str,
    kind: str,
    level: Optional[float],
    title: str,
    message: str,
    price: float,
) -> None:
    """触发条件成立：写入 active_alerts。新触发时立即推送一次。"""
    key = state.make_alert_key(symbol, kind, level)
    is_new, rec = state.store.upsert_active(
        key=key,
        symbol=symbol,
        kind=kind,
        level=level,
        title=title,
        message=message,
        price=price,
    )
    if is_new:
        delivered, receipt = notifier.dispatch(title, message)
        # 写入告警事件流
        state.store.push_alert(state.AlertRecord(
            ts=time.time(),
            symbol=symbol,
            kind=kind,
            title=title,
            message=message,
            price=price,
        ))
        if receipt:
            state.store.set_active_receipt(key, receipt)
        log.info(
            "ARM %s [%s ch=%d receipt=%s]: %s @ %s",
            key, kind, delivered, receipt, title, price,
        )
    else:
        # 不主动重发——交给 reminder 协程节流处理。
        # 仅刷新最新 message/price（在 upsert_active 内部完成）
        log.debug("update active %s @ %s", key, price)


def _disarm(
    symbol: str,
    kind: str,
    level: Optional[float],
    notify_resolve: bool = True,
) -> None:
    """条件解除：删除 active_alerts；可选发送 ✅ 解除通知。"""
    key = state.make_alert_key(symbol, kind, level)
    rec = state.store.resolve_active(key)
    if rec is None:
        return
    if notify_resolve:
        notifier.dispatch(
            f"✅ {symbol.upper()} {rec.kind} 已解除",
            f"({rec.notify_count} 次提醒后) 价格已回归正常",
        )
    log.info("DISARM %s (after %d notifies)", key, rec.notify_count)


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

    keep = max(coin_cfg.flash_crash.window_sec, coin_cfg.slow_crash.window_sec) * 2
    while cs.history and now - cs.history[0][0] > keep:
        cs.history.popleft()

    hyst = coin_cfg.hysteresis_pct / 100.0

    # ---- 跌破阈值 ----
    for level in coin_cfg.price_below_list:
        if price <= level:
            _arm(
                symbol, "below", level,
                f"🚨 {symbol.upper()} 跌破 {level}",
                f"当前价格: {price}",
                price,
            )
        elif price >= level * (1 + hyst):
            _disarm(symbol, "below", level)

    # ---- 突破阈值 ----
    for level in coin_cfg.price_above_list:
        if price >= level:
            _arm(
                symbol, "above", level,
                f"🚀 {symbol.upper()} 突破 {level}",
                f"当前价格: {price}",
                price,
            )
        elif price <= level * (1 - hyst):
            _disarm(symbol, "above", level)

    # ---- 瞬时暴跌 / 持续崩盘 ----
    for kind, rule, emoji, label in (
        ("flash_crash", coin_cfg.flash_crash, "💥", "瞬时暴跌"),
        ("slow_crash", coin_cfg.slow_crash, "☠️", "持续崩盘"),
    ):
        old = _find_old_price(cs.history, now - rule.window_sec)
        if old is None or old <= 0:
            continue
        pct = (price - old) / old * 100
        if pct <= rule.drop_pct:
            _arm(
                symbol, kind, None,
                f"{emoji} {symbol.upper()} {label}",
                f"{rule.window_sec}s 跌幅: {pct:.2f}%\n当前价格: {price}",
                price,
            )
        elif pct > rule.drop_pct + abs(rule.drop_pct) * hyst:
            _disarm(symbol, kind, None)
