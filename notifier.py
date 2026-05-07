"""
多通道通知：Pushover / Telegram / Webhook。

每个 Channel 实现 ``send(title, message) -> bool``。
``dispatch()`` 会依次调用所有启用的 Channel，单点失败不影响其他通道。
"""
from __future__ import annotations

import logging
import time
from typing import List, Protocol

import requests

import config

log = logging.getLogger("tix.notify")

_HTTP_TIMEOUT = 10


class Channel(Protocol):
    name: str

    def send(self, title: str, message: str) -> bool: ...


class PushoverChannel:
    name = "pushover"

    def send(self, title: str, message: str) -> bool:
        cfg = config.get().pushover
        if not cfg.enabled or not cfg.token or not cfg.user:
            return False
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
            log.info("pushover %s -> %s", title, r.status_code)
            return ok
        except requests.RequestException as e:
            log.warning("pushover error: %s", e)
            return False


class TelegramChannel:
    name = "telegram"

    def send(self, title: str, message: str) -> bool:
        cfg = config.get().telegram
        if not cfg.enabled or not cfg.bot_token or not cfg.chat_id:
            return False
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
            return ok
        except requests.RequestException as e:
            log.warning("telegram error: %s", e)
            return False


class WebhookChannel:
    name = "webhook"

    def send(self, title: str, message: str) -> bool:
        cfg = config.get().webhook
        if not cfg.enabled or not cfg.url:
            return False
        try:
            r = requests.post(
                cfg.url,
                json={"title": title, "message": message, "ts": time.time()},
                timeout=_HTTP_TIMEOUT,
            )
            ok = r.status_code < 400
            log.info("webhook %s -> %s", title, r.status_code)
            return ok
        except requests.RequestException as e:
            log.warning("webhook error: %s", e)
            return False


_channels: List[Channel] = [
    PushoverChannel(),
    TelegramChannel(),
    WebhookChannel(),
]

CHANNEL_NAMES = [c.name for c in _channels]


def dispatch(title: str, message: str) -> int:
    """投递到所有启用的通道，返回成功送达的通道数。"""
    delivered = 0
    for ch in _channels:
        try:
            if ch.send(title, message):
                delivered += 1
        except Exception:
            log.exception("channel %s crashed", ch.name)
    return delivered


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
