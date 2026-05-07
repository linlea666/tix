"""
Reminder 协程：定期扫描 ``active_alerts``，做两件事：

1. **检查 Pushover Receipt**：对有 receipt 的未 ack 告警，
   GET /1/receipts/{receipt}.json，若 ``acknowledged=1`` 则把本地 ack 也置 1，停止重发。

2. **间隔重发**：未 ack 且 ``now - last_notify_at >= repeat_interval_sec`` 的，
   再发一条新的紧急消息（priority=2，手机重新响铃），并把新 receipt 写回。

整个循环由 asyncio task 承担，每 5 秒一次。
"""
from __future__ import annotations

import asyncio
import logging
import time

import config
import notifier
import state

log = logging.getLogger("tix.reminder")

POLL_INTERVAL = 5  # 秒：循环节奏


async def _tick() -> None:
    cfg = config.get()
    now = time.time()

    for rec in state.store.list_active():
        coin_cfg = cfg.coins.get(rec.symbol)
        if coin_cfg is None:
            # 币种被删除——清掉残余 active
            state.store.resolve_active(rec.key)
            continue

        # 1) 已有 receipt 且未 ack → 查询 Pushover 是否已被 ack
        if not rec.ack and rec.pushover_receipt:
            try:
                info = await asyncio.to_thread(
                    notifier.query_pushover_receipt, rec.pushover_receipt
                )
            except Exception:
                log.exception("query receipt failed")
                info = None
            if info:
                if info.get("acknowledged"):
                    if state.store.mark_active_acked(
                        rec.key, info.get("acknowledged_at") or now
                    ):
                        log.info(
                            "%s acknowledged via Pushover (after %d notifies)",
                            rec.key, rec.notify_count,
                        )
                    rec.ack = True

        if rec.ack:
            continue

        interval = coin_cfg.repeat_interval_sec
        if interval <= 0:
            continue  # 用户关闭了重发

        if now - rec.last_notify_at < interval:
            continue

        # 2) 重发新消息
        title = f"🔁 {rec.title} (第 {rec.notify_count + 1} 次)"
        try:
            _, receipt = await asyncio.to_thread(
                notifier.dispatch, title, rec.message
            )
        except Exception:
            log.exception("re-dispatch failed for %s", rec.key)
            continue

        state.store.update_active_notify(rec.key, time.time(), receipt)
        log.info(
            "REPEAT %s (#%d, receipt=%s)",
            rec.key, rec.notify_count + 1, receipt,
        )


async def loop(stop: asyncio.Event) -> None:
    log.info("reminder loop started (interval=%ds)", POLL_INTERVAL)
    while not stop.is_set():
        try:
            await _tick()
        except Exception:
            log.exception("reminder tick crashed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL)
            break
        except asyncio.TimeoutError:
            pass
    log.info("reminder loop stopped")
