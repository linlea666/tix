"""
多通道通知：Pushover / Telegram / Webhook。

Pushover Emergency (priority=2) 特殊处理：
- 发送时返回 receipt id，调用方可定期查询 acknowledged 状态。
- 当用户在 Pushover 客户端点击 "Acknowledge" 后，receipts API 返回 acknowledged=1。
- 服务端据此停止重发，但保留 active 记录直到价格 resolve。
"""
from __future__ import annotations

import logging
import time
from typing import List, Optional, Protocol

import requests

import config

log = logging.getLogger("tix.notify")

_HTTP_TIMEOUT = 10


class SendResult:
    __slots__ = ("ok", "receipt")

    def __init__(self, ok: bool, receipt: Optional[str] = None) -> None:
        self.ok = ok
        self.receipt = receipt

    def __bool__(self) -> bool:
        return self.ok


class Channel(Protocol):
    name: str

    def send(self, title: str, message: str) -> SendResult: ...


class PushoverChannel:
    name = "pushover"

    def send(self, title: str, message: str) -> SendResult:
        cfg = config.get().pushover
        if not cfg.enabled or not cfg.token or not cfg.user:
            return SendResult(False)
        try:
            r = requests.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "token": cfg.token,
                    "user": cfg.user,
                    "title": title,
                    "message": message,
                    "priority": cfg.priority,
                    "retry": 60,
                    "expire": 3600,
                    "sound": cfg.sound,
                },
                timeout=_HTTP_TIMEOUT,
            )
            ok = r.status_code == 200
            receipt: Optional[str] = None
            if ok and cfg.priority >= 2:
                try:
                    receipt = r.json().get("receipt")
                except (ValueError, AttributeError):
                    receipt = None
            log.info(
                "pushover %s -> %s (receipt=%s)",
                title, r.status_code, receipt,
            )
            return SendResult(ok, receipt)
        except requests.RequestException as e:
            log.warning("pushover error: %s", e)
            return SendResult(False)

    @staticmethod
    def check_receipt(receipt: str) -> Optional[dict]:
        """查询 Emergency 消息的 receipt 状态。
        返回 dict（含 acknowledged / acknowledged_at / expired 等）；失败返回 None。
        参考: https://pushover.net/api/receipts
        """
        cfg = config.get().pushover
        if not cfg.token or not receipt:
            return None
        try:
            r = requests.get(
                f"https://api.pushover.net/1/receipts/{receipt}.json",
                params={"token": cfg.token},
                timeout=_HTTP_TIMEOUT,
            )
            if r.status_code == 200:
                return r.json()
        except requests.RequestException as e:
            log.warning("pushover receipt query error: %s", e)
        return None


class TelegramChannel:
    name = "telegram"

    def send(self, title: str, message: str) -> SendResult:
        cfg = config.get().telegram
        if not cfg.enabled or not cfg.bot_token or not cfg.chat_id:
            return SendResult(False)
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{cfg.bot_token}/sendMessage",
                data={
                    "chat_id": cfg.chat_id,
                    "text": f"*{title}*\n{message}",
                    "parse_mode": "Markdown",
                    "disable_web_page_preview": "true",
                },
                timeout=_HTTP_TIMEOUT,
            )
            ok = r.status_code == 200
            log.info("telegram %s -> %s", title, r.status_code)
            return SendResult(ok)
        except requests.RequestException as e:
            log.warning("telegram error: %s", e)
            return SendResult(False)


class WebhookChannel:
    name = "webhook"

    def send(self, title: str, message: str) -> SendResult:
        cfg = config.get().webhook
        if not cfg.enabled or not cfg.url:
            return SendResult(False)
        try:
            r = requests.post(
                cfg.url,
                json={"title": title, "message": message, "ts": time.time()},
                timeout=_HTTP_TIMEOUT,
            )
            ok = r.status_code < 400
            log.info("webhook %s -> %s", title, r.status_code)
            return SendResult(ok)
        except requests.RequestException as e:
            log.warning("webhook error: %s", e)
            return SendResult(False)


_pushover = PushoverChannel()
_channels: List[Channel] = [
    _pushover,
    TelegramChannel(),
    WebhookChannel(),
]

CHANNEL_NAMES = [c.name for c in _channels]


def dispatch(title: str, message: str) -> tuple[int, Optional[str]]:
    """投递到所有启用的通道。返回 (成功通道数, pushover_receipt)。"""
    delivered = 0
    receipt: Optional[str] = None
    for ch in _channels:
        try:
            res = ch.send(title, message)
            if res.ok:
                delivered += 1
            if ch.name == "pushover" and res.receipt:
                receipt = res.receipt
        except Exception:
            log.exception("channel %s crashed", ch.name)
    return delivered, receipt


def dispatch_one(channel: str, title: str, message: str) -> bool:
    """只向指定通道投递（用于 UI 单通道测试）。"""
    for ch in _channels:
        if ch.name == channel:
            try:
                return bool(ch.send(title, message))
            except Exception:
                log.exception("channel %s crashed", ch.name)
                return False
    return False


def query_pushover_receipt(receipt: str) -> Optional[dict]:
    return _pushover.check_receipt(receipt)
