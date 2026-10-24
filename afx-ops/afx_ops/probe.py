"""``afx-ops probe <feature>``: enable 前の疎通確認 (本体が絶対 path の subprocess で呼ぶ)。

本体とは終了コードと stderr の固定 reason 1 行 (``afx-ops: <reason>``) だけで結合する。
成功は rc 0 で stdout / stderr とも空。secret・path・設定値・外部応答の本文は出さない。

入力は機能の config dir の ``ops.toml`` と、機能の固定の secret file だけで、どちらも自分で
読む (argv・env・stdin からは受けない)。短命で、利用者が今入力した secret を同じ uid で
確かめるだけなので隔離しない。鍵 dir は読まず、state dir も含めて何も作らない。

| rc | reason |
|---|---|
| 0 | (成功) |
| 2 | ``unknown_feature`` (起動口が返す) |
| 69 | ``probe_not_implemented`` |
| 70 | ``runtime_unavailable`` (表の値を呼べない) |
| 71 | ``config_dir_missing`` / ``config_dir_invalid`` / ``config_invalid`` / ``secrets_failed`` / ``url_invalid`` (notify の webhook URL) / ``token_invalid`` (bot token の形。想定外の失敗は ``startup_internal_error``)。web は公開範囲の ``allow_required`` / ``unrestricted_not_supported`` / ``auth_not_supported`` / ``interface_lookup_failed`` / ``bind_not_local`` / ``secret_invalid`` / ``secret_missing`` / ``secret_unexpected`` |
| 73 | ``probe_bind_failed`` (web) / ``probe_<原因>`` (notify の送信失敗・bot の到達失敗。語は ``afx_ops.notify.probe.REASONS`` / ``afx_ops.bot.probe.REASONS``) |
"""
from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Callable, Mapping
from types import MappingProxyType

from afx_ops.config import ConfigError, OpsConfig, read_config
from afx_ops.isolation import startup
from afx_ops.isolation.accept_loop import ListenerError, prebind_listener

RC_OK = 0
RC_NOT_IMPLEMENTED = 69
RC_RUNTIME_UNAVAILABLE = 70
RC_INPUT_FAILED = 71
RC_PROBE_FAILED = 73

# 機能が読む secret file (起動時の固定 plan と同じ名前)
SECRET_FILES: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {feature: plan.secret_files for feature, plan in startup.PLANS.items()})


class ProbeError(Exception):
    """確認に失敗した。``reason`` は固定語、``rc`` は終了コード。"""

    def __init__(self, reason: str, rc: int = RC_PROBE_FAILED) -> None:
        super().__init__(reason)
        self.reason = reason
        self.rc = rc


def probe_web(config: OpsConfig, secrets: Mapping[str, bytes]) -> None:
    """設定の `<bind>:<port>` を bind + listen でき、`getsockname()` が一致すること。

    設定と公開範囲の組み合わせ (allow・interface の実在・secret) は `_read_inputs` が起動と
    同じ関数で検査済み。accept はせず、すぐ閉じる。
    """
    try:
        listener = prebind_listener(config.port, bind=config.web_bind)
    except (ListenerError, OSError, TypeError):
        raise ProbeError("probe_bind_failed") from None
    listener.close()


def probe_notify(config: OpsConfig, secrets: Mapping[str, bytes]) -> None:
    """webhook へ予約 namespace の probe body を 1 回送る (中身は notify.probe)。"""
    # notify.probe はこの module の ProbeError を使うので、呼ぶときに読む (循環 import を避ける)
    from afx_ops.notify import probe as notify_probe
    notify_probe.probe(config, secrets)


def probe_bot(config: OpsConfig, secrets: Mapping[str, bytes]) -> None:
    """bot token の identity と、設定の guild / channel への到達だけ (中身は bot.probe)。"""
    # bot.probe はこの module の ProbeError を使うので、呼ぶときに読む (循環 import を避ける)
    from afx_ops.bot import probe as bot_probe
    bot_probe.probe(config, secrets)


def probe_not_implemented(config: OpsConfig, secrets: Mapping[str, bytes]) -> None:
    """外向きの疎通確認がまだ無い機能。入力の検査までで止める。"""
    raise ProbeError("probe_not_implemented", RC_NOT_IMPLEMENTED)


PROBES: Mapping[str, Callable[[OpsConfig, Mapping[str, bytes]], None]] = MappingProxyType({
    # bot は token の identity と設定の guild / channel への到達 (interaction は確かめない)
    "bot": probe_bot,
    # web は listener の bind だけを確かめる
    "web": probe_web,
    # notify は送信先の webhook へ probe body を 1 回送る
    "notify": probe_notify,
})


def _read_inputs(feature: str) -> tuple[OpsConfig, Mapping[str, bytes]]:
    config = read_config(feature, preserve_config_dir_error=True)
    try:
        plan = startup.StartupPlan.for_feature(
            feature, web_port=config.port,
            web_bind=config.web_bind if feature == "web" else startup.LOOPBACK,
            web_auth=config.auth)
        if feature == "web":
            startup.check_web_access(plan, config)
        secrets = startup.read_secrets(plan)
    finally:
        os.close(config.config_dir_fd)
    return config.without_config_dir_fd(), secrets


def main(feature: str) -> int:
    try:
        config, secrets = _read_inputs(feature)
        PROBES[feature](config, secrets)
    except startup.StartupError as exc:
        reason, rc = exc.reason, RC_INPUT_FAILED
    except ConfigError as exc:
        reason, rc = exc.reason, RC_INPUT_FAILED
    except ProbeError as exc:
        reason, rc = exc.reason, exc.rc
    except Exception:  # 想定外の失敗も traceback (path や値を含み得る) にしない
        reason, rc = "startup_internal_error", RC_INPUT_FAILED
    else:
        return RC_OK
    with contextlib.suppress(OSError, ValueError):
        print(f"afx-ops: {reason}", file=sys.stderr, flush=True)
    return rc
