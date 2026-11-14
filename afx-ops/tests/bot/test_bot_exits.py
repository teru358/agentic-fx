"""bot unit の終了コードの全分類: 定数、理由ごとの対応、run が返す値、他の入口との衝突の無さ。"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from afx_ops import api_client, cli, probe
from afx_ops.api_client import ApiError
from afx_ops.bot import adapter as adapter_module
from afx_ops.bot import exits, runtime
from afx_ops.bot.adapter import BotFailure
from afx_ops.isolation import accept_loop, startup

TOKEN = "XzIzNDU2Nzg5MDEyMzQ1Njc4OTAx.YWJjZGVm.ZmFrZS1kaXNjb3JkLXRva2VuLWZvci10ZXN0cw"

# 設計の表の全行 (起点 / reason, 終了コード, 分類)
EXISTING_ROWS = [
    ("normal stop", 0, "normal"),
    ("body failure", startup.RC_BODY_FAILED, "unclassified"),
    ("unknown feature", cli.RC_UNKNOWN_FEATURE, "permanent"),
    ("daemon unreachable", api_client.RC_UNREACHABLE, "transient"),
    ("key invalid", api_client.RC_KEY, "permanent"),
    ("feature not implemented", cli.RC_NOT_IMPLEMENTED, "permanent"),
    ("runtime import failure", startup.RC_RUNTIME_IMPORT_FAILED, "permanent"),
    ("startup / isolation failure", startup.RC_ISOLATION_FAILED, "permanent"),
    ("accept exhausted", accept_loop.RC_ACCEPT_EXHAUSTED, "outside"),
    ("probe failed", probe.RC_PROBE_FAILED, "outside"),
    ("accept failed", accept_loop.RC_ACCEPT_FAILED, "outside"),
]
REASON_ROWS = [
    ("token_invalid", 78), ("discord_login_failed", 78), ("bot_state_invalid", 78),
    ("payload_invalid", 78), ("events_invalid", 78), ("high_watermark_regressed", 78),
    ("discord_rejected", 78),
    ("bot_state_unavailable", 79), ("discord_unavailable", 79), ("gateway_failed", 79),
    ("gateway_unhealthy", 79), ("delivery_stalled", 79), ("poller_stalled", 79),
    ("bot_internal_error", 79),
]


def test_the_three_bot_codes_have_the_contract_values():
    assert (exits.RC_BOT_PERMANENT, exits.RC_BOT_TRANSIENT, exits.RC_BOT_LOOP_STALLED) == (78, 79, 80)
    assert (runtime.RC_BOT_PERMANENT, runtime.RC_BOT_TRANSIENT,
            runtime.RC_BOT_LOOP_STALLED) == (78, 79, 80)


def test_the_existing_rows_keep_their_values():
    assert [(row[1]) for row in EXISTING_ROWS] == [0, 1, 2, 3, 4, 69, 70, 71, 72, 73, 74]


def test_the_bot_codes_collide_with_no_existing_entrance_code():
    taken = {row[1] for row in EXISTING_ROWS}
    bot = {exits.RC_BOT_PERMANENT, exits.RC_BOT_TRANSIENT, exits.RC_BOT_LOOP_STALLED}
    assert len(bot) == 3 and not (bot & taken)


@pytest.mark.parametrize("reason, rc", REASON_ROWS)
def test_every_reason_maps_to_its_row(reason, rc):
    assert exits.rc_for_reason(reason) == rc


def test_the_reason_table_is_exactly_the_documented_rows():
    assert set(exits.REASONS) == {reason for reason, _ in REASON_ROWS}
    assert exits.PERMANENT_REASONS == {r for r, rc in REASON_ROWS if rc == 78}
    assert exits.TRANSIENT_REASONS == {r for r, rc in REASON_ROWS if rc == 79}
    assert exits.PERMANENT_REASONS.isdisjoint(exits.TRANSIENT_REASONS)


def test_a_reason_missing_from_the_table_is_restarted_not_pinned_permanent():
    assert exits.rc_for_reason("made_up") == 79


def test_every_failure_reason_raised_in_the_bot_sources_is_in_the_table():
    reasons = set()
    for module in (adapter_module, runtime):
        for node in ast.walk(ast.parse(Path(module.__file__).read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "BotFailure"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                reasons.add(node.args[0].value)
    assert reasons and reasons <= set(exits.REASONS)
    # 進捗の判定が返す理由も表にある
    from afx_ops.bot import progress
    source = Path(progress.__file__).read_text(encoding="utf-8")
    assert set(re.findall(r'return "(\w+)"', source)) <= set(exits.REASONS)


def _isolated(state_dir, secret=TOKEN.encode()):
    return SimpleNamespace(secrets={"discord.token": secret}, state_dir=state_dir, config=None)


class _Out:
    def __init__(self) -> None:
        self.text = ""

    def write(self, data) -> int:
        self.text += data
        return len(data)

    def flush(self) -> None:
        pass


def _run_failing(tmp_path, monkeypatch, exc, secret=TOKEN.encode(), state_dir=None):
    async def main(*args, **kwargs):
        raise exc
    monkeypatch.setattr(runtime, "_main", main)
    out = _Out()
    rc = runtime.run(_isolated(tmp_path if state_dir is None else state_dir, secret),
                     client=object(), stderr=out)
    return rc, out.text


@pytest.mark.parametrize("reason, rc", REASON_ROWS)
def test_run_returns_the_row_code_and_prints_only_the_fixed_reason(tmp_path, monkeypatch, reason, rc):
    got, text = _run_failing(tmp_path, monkeypatch, BotFailure(reason))
    assert (got, text) == (rc, f"afx-ops: {reason}\n")


def test_an_unexpected_exception_is_a_transient_internal_error_with_a_fixed_word(tmp_path, monkeypatch):
    got, text = _run_failing(tmp_path, monkeypatch, ValueError("secret-value"))
    assert (got, text) == (79, "afx-ops: bot_internal_error\n")


def test_an_unreachable_daemon_stays_rc_3(tmp_path, monkeypatch):
    got, text = _run_failing(tmp_path, monkeypatch, ApiError("unreachable"))
    assert got == api_client.RC_UNREACHABLE == 3 and "unreachable" in text


def test_a_malformed_token_is_permanent_before_anything_starts(tmp_path, monkeypatch):
    got, text = _run_failing(tmp_path, monkeypatch, RuntimeError("not reached"), secret=b"nope")
    assert (got, text) == (78, "afx-ops: token_invalid\n")


def test_a_state_dir_that_cannot_be_opened_is_a_transient_io_failure(tmp_path, monkeypatch):
    got, text = _run_failing(tmp_path, monkeypatch, RuntimeError("not reached"),
                             state_dir=tmp_path / "missing")
    assert (got, text) == (79, "afx-ops: bot_state_unavailable\n")


def test_a_clean_return_is_zero(tmp_path, monkeypatch):
    async def main(*args, **kwargs):
        return None
    monkeypatch.setattr(runtime, "_main", main)
    assert runtime.run(_isolated(tmp_path), client=object(), stderr=_Out()) == 0
