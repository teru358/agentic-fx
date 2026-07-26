"""Discord webhook 通知 (移植簡素版)。失敗しても取引経路を止めない。"""
from __future__ import annotations

import json
import logging
import urllib.request

_log = logging.getLogger("agentic_fx.notifier")


class Notifier:
    def __init__(self, enabled: bool, webhook_url: str | None) -> None:
        self._enabled = enabled and bool(webhook_url)
        self._url = webhook_url

    def send(self, text: str) -> None:
        if not self._enabled:
            return
        try:
            req = urllib.request.Request(
                self._url,
                data=json.dumps({"content": text[:1900]}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:  # noqa: BLE001 — 通知失敗は warning のみ
            _log.warning("discord notify failed: %s", e)
