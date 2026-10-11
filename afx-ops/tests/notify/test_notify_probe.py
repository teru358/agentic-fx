"""notify の疎通確認: 予約 namespace の probe body を 1 回だけ送り、戻りは rc と固定 reason。

network は fake の送信で置き換える。実 TLS の webhook への送信は test_notify_isolated。
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

from afx_ops import probe as probe_cli
from afx_ops.config import OpsConfig
from afx_ops.notify import probe as notify_probe
from afx_ops.notify.delivery import DeliveryError
from afx_ops.notify.payload import ALLOWED_FIELDS, PayloadError, encode_event

URL = b"https://hooks.example.test/afx/probe-7d1e"
CONFIG = OpsConfig(instance_root=Path("/nonexistent-instance"), key_dir=Path("/nonexistent-keys"),
                   config_dir_fd=None)


class Send:
    def __init__(self, result=None) -> None:
        self.result = result
        self.calls: list[tuple[object, dict]] = []

    def __call__(self, target, body: bytes) -> None:
        self.calls.append((target, json.loads(body)))
        if self.result is not None:
            raise self.result


def test_probe_payload_is_the_reserved_schema_only():
    payload = notify_probe.probe_payload()
    assert set(payload) == {"code", "probe", "nonce", "sent_at"}
    assert payload["code"] == notify_probe.PROBE_CODE == "afx.probe.notify"
    assert payload["probe"] is True
    assert re.fullmatch(r"[0-9a-f]{32}", payload["nonce"])
    assert datetime.fromisoformat(payload["sent_at"]).tzinfo is not None
    assert notify_probe.probe_payload()["nonce"] != payload["nonce"]


def test_event_schema_never_uses_the_probe_namespace():
    assert not any(code.startswith("afx.probe.") for code in ALLOWED_FIELDS)
    # probe の body は event の encoder を通らない (event として偽装できない)
    payload = notify_probe.probe_payload()
    with pytest.raises(PayloadError):
        encode_event({"id": 1, "ts": payload["sent_at"], "code": payload["code"],
                      "ref": "instance", "fields": {"probe": True, "nonce": payload["nonce"],
                                                     "sent_at": payload["sent_at"]}})
    assert not {"event_id", "ref", "ts", "id"} & set(payload)


def test_probe_sends_once_to_the_validated_target():
    send = Send()
    notify_probe.probe(CONFIG, {"webhook.url": URL}, send=send)
    assert len(send.calls) == 1
    target, body = send.calls[0]
    assert (target.host, target.port) == ("hooks.example.test", 443)
    assert body["code"] == "afx.probe.notify"


@pytest.mark.parametrize("url", [b"http://hooks.example.test/x", b"https://h:8443/x",
                                 b"https://u@h/x", b"\xff", b"not a url"])
def test_invalid_url_is_an_input_failure_without_sending(url):
    send = Send()
    with pytest.raises(probe_cli.ProbeError) as raised:
        notify_probe.probe(CONFIG, {"webhook.url": url}, send=send)
    assert (raised.value.reason, raised.value.rc) == ("url_invalid", probe_cli.RC_INPUT_FAILED)
    assert send.calls == []


@pytest.mark.parametrize("cause, retryable", [
    ("dns_failed", True), ("connect_failed", True), ("tls_failed", True), ("timeout", True),
    ("connection_lost", True), ("http_5xx", True), ("http_429", True), ("redirect", False),
    ("http_4xx", False), ("response_invalid", False), ("transport_failed", False)])
def test_reachability_failure_is_rc_73_with_a_fixed_reason_and_no_retry(cause, retryable):
    send = Send(DeliveryError(cause, retryable=retryable))
    with pytest.raises(probe_cli.ProbeError) as raised:
        notify_probe.probe(CONFIG, {"webhook.url": URL}, send=send)
    assert raised.value.rc == probe_cli.RC_PROBE_FAILED == 73
    assert raised.value.reason == f"probe_{cause}"
    assert raised.value.reason in notify_probe.REASONS
    assert len(send.calls) == 1


def test_probe_table_row_calls_the_notify_probe(monkeypatch):
    assert probe_cli.PROBES["notify"] is probe_cli.probe_notify
    seen = []
    monkeypatch.setattr(notify_probe, "probe", lambda config, secrets: seen.append(secrets))
    probe_cli.PROBES["notify"](CONFIG, {"webhook.url": URL})
    assert seen == [{"webhook.url": URL}]


def test_probe_does_not_touch_state_cursor_or_api_keys():
    source = Path(notify_probe.__file__).read_text(encoding="utf-8")
    for word in ("state", "cursor", "snapshot", "api_token", "key_dir", "client_for",
                 "NotifyClient"):
        assert word not in source
