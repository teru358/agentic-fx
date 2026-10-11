"""`afx-ops probe notify`: webhook へ予約 namespace の probe body を 1 回だけ送る。

body は `{"code": "afx.probe.notify", "probe": true, "nonce": <32 hex>, "sent_at": <ISO 8601>}`
だけで、event の encoder を通さない (event の code / id / ref を名乗らない)。受け手は
`afx.probe.` で始まる code で本番の event と区別できる。

送信は runtime と同じ 1 回送信 (HTTPS、443、証明書と hostname の検証、redirect 不追従、
2xx だけ成功) を使い、再送しない。URL の不正は入力の失敗 (rc 71、`url_invalid`)、
送れなかったことは rc 73 の `probe_<原因>` だけを返し、URL や応答本文は出さない。
"""
from __future__ import annotations

import secrets as _secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from afx_ops.notify import transport
from afx_ops.notify.delivery import DeliveryError, encode_json_object, validate_webhook_url
from afx_ops.probe import RC_INPUT_FAILED, ProbeError

if TYPE_CHECKING:
    from afx_ops.config import OpsConfig

PROBE_CODE = "afx.probe.notify"
SECRET_NAME = "webhook.url"

_CAUSES = ("dns_failed", "connect_failed", "tls_failed", "timeout", "connection_lost",
           "redirect", "http_429", "http_4xx", "http_5xx", "response_invalid",
           "transport_failed")
REASONS = ("url_invalid", *(f"probe_{cause}" for cause in _CAUSES))


def probe_payload() -> dict[str, object]:
    return {"code": PROBE_CODE, "probe": True, "nonce": _secrets.token_hex(16),
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def probe(_config: OpsConfig, secrets: Mapping[str, bytes], *,
          send: Callable[..., None] | None = None) -> None:
    """send は試験だけが渡す。既定は runtime と同じ `transport.post`。

    `_config` は他 feature の probe と呼び口を揃えるためだけにあり、使わない。
    """
    try:
        target = validate_webhook_url(secrets[SECRET_NAME])
    except (DeliveryError, KeyError, TypeError):
        raise ProbeError("url_invalid", RC_INPUT_FAILED) from None
    body = encode_json_object(probe_payload())
    try:
        (transport.post if send is None else send)(target, body)
    except DeliveryError as exc:
        if exc.reason == "url_invalid":
            raise ProbeError("url_invalid", RC_INPUT_FAILED) from None
        cause = exc.reason if exc.reason in _CAUSES else "transport_failed"
        raise ProbeError(f"probe_{cause}") from None
