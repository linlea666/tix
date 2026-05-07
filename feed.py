"""
Binance Combined Stream：单条 WebSocket 复用所有币种 ticker。

参考: https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams
URL 形如: wss://stream.binance.com:9443/stream?streams=btcusdt@ticker/ethusdt@ticker

特性：
- asyncio + websockets 库，单事件循环、单连接；币种数从几十到几百都不用再开线程。
- 心跳：ping_interval/ping_timeout 由 websockets 自动维护。
- 断线指数退避（1s → 60s 上限），并在重连时刷新连接状态。
- 配置变更（增删币种）由 ``FeedManager.refresh()`` 平滑切换。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import List, Tuple

import websockets

import config
import monitor
import state

log = logging.getLogger("tix.feed")

BASE_URL = "wss://stream.binance.com:9443/stream"
PING_INTERVAL = 20
PING_TIMEOUT = 10
MAX_BACKOFF = 60


async def _consume(symbols: List[str], stop: asyncio.Event) -> None:
    if not symbols:
        log.info("no symbols configured; feed idle")
        await stop.wait()
        return

    streams = "/".join(f"{s}@ticker" for s in symbols)
    url = f"{BASE_URL}?streams={streams}"
    backoff = 1

    while not stop.is_set():
        try:
            async with websockets.connect(
                url,
                ping_interval=PING_INTERVAL,
                ping_timeout=PING_TIMEOUT,
                close_timeout=5,
                max_size=2 ** 20,
            ) as ws:
                log.info("WS connected: %d streams", len(symbols))
                for s in symbols:
                    state.store.set_connected(s, True)
                backoff = 1

                async for raw in ws:
                    if stop.is_set():
                        break
                    try:
                        msg = json.loads(raw)
                        data = msg.get("data") or {}
                        sym = (data.get("s") or "").lower()
                        c = data.get("c")
                        if sym and c is not None:
                            monitor.process_price(sym, float(c))
                    except (ValueError, KeyError, TypeError):
                        log.exception("bad ws message")
        except asyncio.CancelledError:
            raise
        except (websockets.WebSocketException, OSError) as e:
            log.warning("WS dropped: %s; reconnect in %ds", e, backoff)
        except Exception:
            log.exception("WS unexpected error; reconnect in %ds", backoff)
        finally:
            for s in symbols:
                state.store.set_connected(s, False)

        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
            break  # stop 在等待中被设置 -> 退出循环
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, MAX_BACKOFF)


class FeedManager:
    """监控配置中的币种列表；变更时平滑重启 WS 任务。"""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event = asyncio.Event()
        self._current: Tuple[str, ...] = ()
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        await self.refresh()

    async def refresh(self) -> None:
        async with self._lock:
            symbols = tuple(sorted(config.get().coins.keys()))
            if (
                symbols == self._current
                and self._task is not None
                and not self._task.done()
            ):
                return

            await self._stop_current()

            self._stop = asyncio.Event()
            self._current = symbols
            log.info("feed (re)starting for: %s", symbols or "<empty>")
            self._task = asyncio.create_task(
                _consume(list(symbols), self._stop),
                name="tix-feed",
            )

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_current()
            self._current = ()

    async def _stop_current(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except asyncio.TimeoutError:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None


manager = FeedManager()
