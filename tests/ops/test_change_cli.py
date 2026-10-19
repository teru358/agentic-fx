"""`afx ctl approval <id>` の変更要約 (diff は出さない、整形は redactor 経由)。"""
from __future__ import annotations

import pytest

from agentic_fx.ops import change_limits as L
from agentic_fx.ops import client

from ._change_fixtures import (ADVERSARIAL_PLUGIN, ADVERSARIAL_SECRETS, SECRET_TOKEN,
                               approval_payload, insert_approval, make_live, make_staging,
                               make_version)
from ._ctl import ctl_api, run_ctl  # noqa: F401
from .conftest import OPERATOR

NAME = "sma_cross"
OLD_CFG = "kind: strategy\nparams:\n  period: 14\n  note: old\n"
NEW_CFG = "kind: strategy\nparams:\n  period: 10\n  note: new\n"


def _detail(env, new_cfg=NEW_CFG, plugin_py="x = 2\n", **payload_extra):
    plugins = env.plugins_root
    old_hash = make_version(plugins, NAME, plugin_py="x = 1\n", config=OLD_CFG)
    make_live(plugins, NAME, old_hash)
    digest = make_staging(plugins, 7, NAME, plugin_py=plugin_py, config=new_cfg)
    payload = approval_payload(NAME, digest=digest)
    payload.update(payload_extra)
    approval_id = insert_approval(env, payload)
    return approval_id, env.service().approval_detail(OPERATOR, approval_id), old_hash, digest


def test_ac29_ctl_approval_shows_the_change_summary_without_the_diff(ctl_api):
    env = ctl_api.env
    approval_id, _, old_hash, new_hash = _detail(env, plugin_py="SENTINEL_BODY = 2\n")
    rc, out, _ = run_ctl(env.root, ["approval", str(approval_id)])
    assert rc == 0, out
    assert "-- 変更内容 --" in out
    assert f"plugin: {NAME} (strategy) 分類: 更新" in out
    assert f"候補: {new_hash[:12]} 比較対象: {old_hash[:12]}" in out
    assert "params の差分:" in out and "params.period: 14 → 10" in out
    assert "params.note: old → new" in out
    assert "@@" not in out and "SENTINEL_BODY" not in out and "+x = 2" not in out
    assert "payload_sha256=" in out


def test_ac29_new_candidate_says_there_is_no_previous_version(ops_env):
    digest = make_staging(ops_env.plugins_root, 7, NAME, config=NEW_CFG)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=digest))
    text = client.format_approval_detail(ops_env.service().approval_detail(OPERATOR, approval_id))
    assert "分類: 新規 (前版なし)" in text and "params の差分:" not in text
    assert "比較対象: -" in text


def test_ac29_detail_from_an_older_daemon_without_change_still_formats():
    text = client.format_approval_detail({"id": 1, "kind": "plugin", "status": "pending"})
    assert "-- 変更内容 --" not in text and "approval #1" in text


@pytest.mark.parametrize("length,truncated", [(L.PARAM_VALUE_CHARS - 1, False),
                                              (L.PARAM_VALUE_CHARS, False),
                                              (L.PARAM_VALUE_CHARS + 1, True)])
def test_ac5_cli_value_cut_at_the_display_limit(length, truncated):
    value = "v" * length
    text = client.format_approval_detail({"id": 1, "change": {
        "classification": "updated", "params": {"items": [{"key": "k", "old": "a", "new": value}]}}})
    line = next(x for x in text.splitlines() if x.startswith("  k:"))
    shown = line.split(" → ", 1)[1]
    assert (shown == "v" * L.PARAM_VALUE_CHARS + "…") is truncated
    assert (shown == value) is (not truncated)


def test_ac23a_cli_redacts_before_cutting_so_a_token_at_the_limit_cannot_survive():
    value = "&" * (L.PARAM_VALUE_CHARS - len(L.REDACTED) - 1) + " " + SECRET_TOKEN
    text = client.format_approval_detail({"id": 1, "change": {
        "classification": "updated", "params": {"items": [{"key": "k", "old": "a", "new": value}]}}})
    assert SECRET_TOKEN not in text and "ghp_" not in text
    line = next(x for x in text.splitlines() if x.startswith("  k:"))
    assert L.REDACTED in line


def test_ac22a_cli_never_prints_adversarial_values(ops_env):
    secrets = ADVERSARIAL_SECRETS
    approval_id, detail, _, _ = _detail(
        ops_env, new_cfg=NEW_CFG + f"  where: '{secrets[0]} {secrets[3]}'\n",
        plugin_py=ADVERSARIAL_PLUGIN,
        agent_claims={"summary": "at " + secrets[0] + " " + secrets[5],
                      "selection_rationale": "host " + secrets[1] + " ip " + secrets[3]})
    # daemon 側の redaction を信用せず、CLI 自身の redactor が掛かることも確かめる
    detail["facts_lines"] = ["自己申告: summary: at " + secrets[0] + " " + secrets[5]]
    detail["reason"] = "see " + secrets[2] + " " + secrets[6]
    detail["change"]["params"]["items"].append(
        {"key": "raw", "old": secrets[4], "new": "password = " + secrets[7] + " " + secrets[8]})
    text = client.format_approval_detail(detail)
    for secret in secrets:
        assert secret not in text, secret
    assert L.REDACTED in text


def test_ac22a_cli_does_not_redact_the_digest_or_hashes(ops_env):
    _, detail, old_hash, new_hash = _detail(ops_env)
    text = client.format_approval_detail(detail)
    assert f"payload_sha256={detail['payload_sha256']}" in text
    assert new_hash[:12] in text and old_hash[:12] in text


@pytest.mark.parametrize("code", [L.UNAVAILABLE, L.HASH_MISMATCH, L.TOO_LARGE, L.UNSAFE_SOURCE,
                                  L.REDACTED_CODE])
def test_ac31a_cli_prints_only_the_fixed_text_for_each_reason_code(code):
    text = client.format_approval_detail({"id": 1, "change": {
        "classification": "unknown", "reason": code, "params": {"items": []}}})
    assert L.REASON_TEXT[code] in text
    others = {t for c, t in L.REASON_TEXT.items() if t != L.REASON_TEXT[code]}
    assert not any(other in text for other in others)


def test_ac31a_cli_unknown_reason_code_falls_back_to_a_fixed_text():
    text = client.format_approval_detail({"id": 1, "change": {
        "classification": "unknown", "reason": "free text /etc/passwd", "params": {"items": []}}})
    assert L.REASON_TEXT[L.UNSAFE_SOURCE] in text and "passwd" not in text


def test_ac31a_cli_params_redacted_is_per_params_and_keeps_the_rest(ops_env):
    text = client.format_approval_detail({"id": 1, "change": {
        "classification": "updated", "name": NAME, "kind": "strategy", "reason": None,
        "params": {"items": [], "reason": L.REDACTED_CODE}}})
    assert L.REASON_TEXT[L.REDACTED_CODE] in text and f"plugin: {NAME}" in text


def test_ac26_cli_prints_exactly_one_label_per_classification():
    for cls, label in L.CLASS_LABELS.items():
        text = client.format_approval_detail({"id": 1, "change": {
            "classification": cls, "params": {"items": []}}})
        assert f"分類: {label}" in text
        assert "同一版・変更なし" not in text


@pytest.mark.parametrize("change", [
    {"classification": []}, {"classification": "updated", "reason": {}},
    {"classification": {}, "reason": []}, {"classification": "updated", "params": {"items": "x"}},
    {"classification": "updated", "params": []},
])
def test_malformed_change_from_the_daemon_degrades_to_one_fixed_line(change):
    lines = client.format_change_lines(change)
    assert lines == ["-- 変更内容 --", client.INVALID_CHANGE_TEXT]
    text = client.format_approval_detail({"id": 1, "kind": "plugin", "status": "pending",
                                          "change": change})
    assert client.INVALID_CHANGE_TEXT in text
