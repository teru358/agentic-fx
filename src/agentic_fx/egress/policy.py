"""改善 mission の CLI が外へ出てよい先の定数。

隔離を緩める設定 key は作らない。ここを変えることは隔離の契約を変えることなので、
正規形の hash を fingerprint の材料にして、変更が起動時に検出されるようにする。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping

# backend ごとの CONNECT 許可 host。telemetry の宛先は意図して入れない。
# opencode は proxy を通る外向き host を持たず、llama-swap への直接 connect だけを使う。
EGRESS_HOSTS: Mapping[str, frozenset[str]] = {
    "claude": frozenset({"api.anthropic.com"}),
    "codex": frozenset({"chatgpt.com", "ab.chatgpt.com"}),
    "opencode": frozenset(),
}

# opencode 専用の直接 connect 先と、CLI の env に渡す llama-swap URL の literal。
OPENCODE_DIRECT_TARGET: tuple[str, int] = ("127.0.0.1", 8080)
LLAMA_SWAP_URL = "http://127.0.0.1:8080/v1"

CONNECT_PORT = 443
RESOLVER_MAX_THREADS = 4
RESOLVER_TIMEOUT_SEC = 5.0


def connect_hosts(backend: str) -> frozenset[str]:
    return EGRESS_HOSTS.get(backend, frozenset())


def is_connect_host_allowed(backend: str, host: str) -> bool:
    return host in connect_hosts(backend)


def _canonical(hosts: Mapping[str, Iterable[str]]) -> bytes:
    body = {
        "hosts": {b: sorted(set(h)) for b, h in sorted(hosts.items())},
        "opencode_direct": list(OPENCODE_DIRECT_TARGET),
        "llama_swap_url": LLAMA_SWAP_URL,
        "port": CONNECT_PORT,
        "resolver_threads": RESOLVER_MAX_THREADS,
        "resolver_timeout_sec": RESOLVER_TIMEOUT_SEC,
    }
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def egress_constants_digest(hosts: Mapping[str, Iterable[str]] | None = None) -> str:
    """egress 定数の正規形の SHA-256 (hex)。hosts はテストで差し替えるための引数。"""
    return hashlib.sha256(_canonical(EGRESS_HOSTS if hosts is None else hosts)).hexdigest()
