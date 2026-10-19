"""変更内容の API (approval_detail.change と GET /v1/approvals/{id}/change)。"""
from __future__ import annotations

import json
import logging
import os

import pytest

from agentic_fx.ops import change as change_mod
from agentic_fx.ops import change_limits as L

from ._change_fixtures import (ADVERSARIAL_PLUGIN, ADVERSARIAL_SECRETS, CONFIG, PLUGIN_PY,
                               TEST_PY, approval_payload, insert_approval, insert_journal,
                               make_archive, make_human, make_live, make_staging, make_version,
                               hash_of, write_files)
from .conftest import OPERATOR, digest_of

NAME = "sma_cross"

OLD_CFG = """kind: strategy
timeframe: 1h
pairs: [USDJPY]
exit_mode: levels
max_bars: 200
indicators:
  rsi:
    plugin: rsi_indicator
    params:
      period: 14
params:
  oversold: 30
  overbought: 70
"""
NEW_CFG = OLD_CFG.replace("period: 14", "period: 10").replace("oversold: 30", "oversold: 25") \
    .replace("max_bars: 200", "max_bars: 300").replace("[USDJPY]", "[USDJPY, EURUSD]")


def _change(env, approval_id):
    return env.service().approval_change(OPERATOR, approval_id)


def _detail(env, approval_id):
    return env.service().approval_detail(OPERATOR, approval_id)


def _pending_update(env, old_cfg=OLD_CFG, new_cfg=NEW_CFG, old_test=TEST_PY, new_test=TEST_PY,
                    **payload_extra):
    plugins = env.plugins_root
    old_hash = make_version(plugins, NAME, config=old_cfg, test=old_test)
    make_live(plugins, NAME, old_hash)
    new_hash = make_staging(plugins, 7, NAME, config=new_cfg, test=new_test)
    approval_id = insert_approval(env, approval_payload(NAME, digest=new_hash, **payload_extra))
    return approval_id, old_hash, new_hash


def _items(change):
    return {item["key"]: (item["old"], item["new"]) for item in change["params"]["items"]}


# ---------------------------------------------------------------- AC-1 / 2 / 3 / 4 / 25 / 26

def test_ac1_new_candidate_reports_name_kind_class_performance_and_summary(ops_env):
    new_hash = make_staging(ops_env.plugins_root, 7, NAME, config=NEW_CFG)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=new_hash))
    before = digest_of(ops_env, approval_id)

    detail = _detail(ops_env, approval_id)
    change = detail["change"]

    assert change["name"] == NAME and change["kind"] == "strategy"
    assert change["classification"] == L.CLASS_NEW and change["reason"] is None
    assert change["baseline_hash"] is None and change["candidate_hash"] == new_hash
    assert change["params"]["items"] == []
    assert change["summary"] == "期間を 5 から 8 に変更"
    assert change["performance"]["in_sample"] == [
        {"pair": "USDJPY", "trades": 40, "pf": 1.31, "avg_r": 0.12}]
    assert change["performance"]["holdout"][0]["trades"] == 20
    assert detail["payload_sha256"] == before == digest_of(ops_env, approval_id)


def test_ac1_detail_stays_light_and_has_no_diff_body(ops_env):
    approval_id, _, _ = _pending_update(ops_env)
    detail = _detail(ops_env, approval_id)
    assert "diff" not in detail and "diff" not in detail["change"]
    assert "files" not in json.dumps(detail["change"])


def test_ac2_updated_candidate_reports_baseline_and_param_diff(ops_env):
    approval_id, old_hash, new_hash = _pending_update(ops_env)
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UPDATED
    assert change["baseline_hash"] == old_hash and change["candidate_hash"] == new_hash
    assert _items(change) == {
        "indicators.rsi.params.period": ("14", "10"),
        "max_bars": ("200", "300"),
        "params.oversold": ("30", "25"),
        "pairs": ('["USDJPY"]', '["USDJPY", "EURUSD"]'),
    }


def test_ac3_plain_dir_is_unknown_and_absent_is_new(ops_env):
    plugins = ops_env.plugins_root
    new_hash = make_staging(plugins, 7, NAME, config=NEW_CFG)
    (plugins / NAME).mkdir(parents=True)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=new_hash))
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UNKNOWN and change["reason"] is None
    assert change["params"]["items"] == []
    assert change["candidate_hash"] == new_hash and change["performance"]["in_sample"]
    assert change["summary"]
    diff = _change(ops_env, approval_id)["diff"]
    assert diff["files"] == []
    (plugins / NAME).rmdir()
    assert _detail(ops_env, approval_id)["change"]["classification"] == L.CLASS_NEW


def test_ac4_same_content_hash_different_test_is_updated_with_test_diff(ops_env):
    approval_id, old_hash, new_hash = _pending_update(
        ops_env, new_cfg=OLD_CFG, new_test="def test_x():\n    assert True\n")
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UPDATED and old_hash != new_hash
    files = {f["path"]: f for f in _change(ops_env, approval_id)["diff"]["files"]}
    assert files["plugin.py"]["status"] == "unchanged"
    assert files["config.yaml"]["status"] == "unchanged"
    lines = [line for hunk in files["test_plugin.py"]["hunks"] for line in hunk["lines"]]
    assert "+    assert True" in lines and "-    pass" in lines


def test_ac26_same_artifact_is_same_without_diff(ops_env):
    plugins = ops_env.plugins_root
    digest = make_version(plugins, NAME, config=NEW_CFG)
    make_live(plugins, NAME, digest)
    make_staging(plugins, 7, NAME, config=NEW_CFG)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=digest))
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_SAME and change["baseline_hash"] == digest
    assert {f["status"] for f in _change(ops_env, approval_id)["diff"]["files"]} == {"unchanged"}


def test_ac25_hashes_and_computed_time_are_in_the_response(ops_env):
    approval_id, old_hash, new_hash = _pending_update(ops_env)
    expected_time = ops_env.wall().isoformat()
    for change in (_detail(ops_env, approval_id)["change"],
                   _change(ops_env, approval_id)["change"]):
        assert (change["candidate_hash"], change["baseline_hash"]) == (new_hash, old_hash)
        assert change["computed_at"] == expected_time


def test_ac26_classification_enum_and_labels_are_one_to_one():
    assert L.CLASSES == ("new", "updated", "same", "unknown")
    assert L.CLASS_LABELS == {"new": "新規", "updated": "更新", "same": "同一版", "unknown": "不明"}
    assert not any("変更なし" in label for label in L.CLASS_LABELS.values())


def _decided(env, old_kind, old_target, new_cfg=NEW_CFG):
    plugins = env.plugins_root
    new_hash = make_version(plugins, NAME, config=new_cfg)
    make_live(plugins, NAME, new_hash)
    approval_id = insert_approval(env, approval_payload(NAME, digest=new_hash), status="approved")
    return approval_id, new_hash


def test_ac26_decided_approval_uses_the_journal_old_version_as_baseline(ops_env):
    old_hash = make_version(ops_env.plugins_root, NAME, config=OLD_CFG)
    approval_id, new_hash = _decided(ops_env, None, None)
    insert_journal(ops_env, approval_id, NAME, "symlink", f".versions/{NAME}/{old_hash}")
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UPDATED and change["baseline_hash"] == old_hash
    assert change["params"]["items"]


def test_ac26_decided_absent_old_kind_is_new(ops_env):
    approval_id, _ = _decided(ops_env, None, None)
    insert_journal(ops_env, approval_id, NAME, "absent", None)
    assert _detail(ops_env, approval_id)["change"]["classification"] == L.CLASS_NEW


def test_ac26_conflicting_decided_journal_rows_fall_back_to_unknown(ops_env):
    a = make_version(ops_env.plugins_root, NAME, config=OLD_CFG)
    b = make_version(ops_env.plugins_root, NAME, config=OLD_CFG + "# b\n")
    approval_id, _ = _decided(ops_env, None, None)
    insert_journal(ops_env, approval_id, NAME, "symlink", f".versions/{NAME}/{a}")
    insert_journal(ops_env, approval_id, NAME, "symlink", f".versions/{NAME}/{b}")
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UNKNOWN and change["params"]["items"] == []


def test_ac26_agreeing_rows_and_non_decided_rows_use_the_last_decided_row(ops_env):
    a = make_version(ops_env.plugins_root, NAME, config=OLD_CFG)
    other = make_version(ops_env.plugins_root, NAME, config=OLD_CFG + "# other\n")
    approval_id, _ = _decided(ops_env, None, None)
    insert_journal(ops_env, approval_id, NAME, "symlink", f".versions/{NAME}/{other}",
                   phase="switched")
    insert_journal(ops_env, approval_id, NAME, "symlink", f".versions/{NAME}/{a}")
    insert_journal(ops_env, approval_id, NAME, "symlink", f".versions/{NAME}/{a}")
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UPDATED and change["baseline_hash"] == a


@pytest.mark.parametrize("old_target", [
    "../.versions/sma_cross/{h}", ".versions/other/{h}", ".versions/sma_cross/{short}",
    "/abs/.versions/sma_cross/{h}", ".versions/sma_cross/../sma_cross/{h}"])
def test_ac24_journal_old_target_must_match_exactly(ops_env, old_target):
    h = make_version(ops_env.plugins_root, NAME, config=OLD_CFG)
    approval_id, _ = _decided(ops_env, None, None)
    insert_journal(ops_env, approval_id, NAME, "symlink",
                   old_target.format(h=h, short=h[:63]))
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UNKNOWN and change["reason"] == L.UNSAFE_SOURCE
    assert change["params"]["items"] == []
    assert _change(ops_env, approval_id)["diff"]["files"] == []
    assert change["candidate_hash"] and change["performance"]["in_sample"]


@pytest.mark.parametrize("target", [
    "../.versions/sma_cross/{h}", ".versions/other/{h}", ".versions/sma_cross/{short}",
    "/abs/.versions/sma_cross/{h}", ".versions/sma_cross/{long}", ".versions/sma_cross/{upper}"])
def test_ac24_live_symlink_target_must_match_exactly(ops_env, target):
    plugins = ops_env.plugins_root
    h = make_version(plugins, NAME, config=OLD_CFG)
    new_hash = make_staging(plugins, 7, NAME, config=NEW_CFG)
    os.symlink(target.format(h=h, short=h[:63], long=h + "0", upper=h.upper()), plugins / NAME)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=new_hash))
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UNKNOWN and change["reason"] == L.UNSAFE_SOURCE
    assert change["baseline_hash"] is None and change["params"]["items"] == []
    assert change["candidate_hash"] == new_hash and change["summary"]


def test_ac24_live_symlink_to_tampered_version_is_unknown(ops_env):
    plugins = ops_env.plugins_root
    h = make_version(plugins, NAME, config=OLD_CFG)
    make_live(plugins, NAME, h)
    leaf = plugins / ".versions" / NAME / h
    os.chmod(leaf / "config.yaml", 0o600)
    (leaf / "config.yaml").write_text(OLD_CFG + "# edited\n")
    new_hash = make_staging(plugins, 7, NAME, config=NEW_CFG)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=new_hash))
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UNKNOWN and change["reason"] == L.HASH_MISMATCH


# ---------------------------------------------------------------- AC-5 params

def _cfg(**params) -> str:
    body = "".join(f"  {k}: {v}\n" for k, v in params.items())
    return f"kind: strategy\nparams:\n{body}"


def test_ac5_keys_outside_the_allowed_pattern_are_counted_not_shown(ops_env):
    old = "kind: strategy\nparams:\n  ok: 1\n  'bad key': 1\n  'x/y': 1\n"
    new = "kind: strategy\nparams:\n  ok: 2\n  'bad key': 2\n  'x/y': 2\n"
    approval_id, _, _ = _pending_update(ops_env, old_cfg=old, new_cfg=new)
    params = _detail(ops_env, approval_id)["change"]["params"]
    assert [item["key"] for item in params["items"]] == ["params.ok"]
    assert params["invalid_keys"] == 2


def _nested(depth: int, value) -> str:
    inner = str(value)
    for level in range(depth, 0, -1):
        inner = "{" + f"k{level}: {inner}" + "}"
    return f"kind: strategy\nparams:\n  root: {inner}\n"


@pytest.mark.parametrize("depth,leaf_key,collapsed", [
    (L.FLATTEN_MAX_DEPTH - 2, "params.root.k1.k2", False),
    (L.FLATTEN_MAX_DEPTH - 1, "params.root.k1.k2.k3", False),
    (L.FLATTEN_MAX_DEPTH, "params.root.k1.k2.k3", True),
])
def test_ac5_flatten_depth_boundary(ops_env, depth, leaf_key, collapsed):
    approval_id, _, _ = _pending_update(ops_env, old_cfg=_nested(depth, 1),
                                        new_cfg=_nested(depth, 2))
    items = _items(_detail(ops_env, approval_id)["change"])
    assert list(items) == [leaf_key]
    if collapsed:
        # 最大深さを超える入れ子は 1 つの項目にまとめる (値は入れ子の JSON)
        assert "k4" in items[leaf_key][0] and items[leaf_key][0] != items[leaf_key][1]
    else:
        assert items[leaf_key] == ("1", "2")


def test_ac5_deeper_than_max_depth_collapses_into_one_item(ops_env):
    approval_id, _, _ = _pending_update(
        ops_env, old_cfg=_nested(L.FLATTEN_MAX_DEPTH + 2, 1),
        new_cfg=_nested(L.FLATTEN_MAX_DEPTH + 2, 2))
    items = _items(_detail(ops_env, approval_id)["change"])
    assert len(items) == 1
    key = next(iter(items))
    assert len(key.split(".")) == 1 + 1 + L.FLATTEN_MAX_DEPTH - 1
    assert "k5" in items[key][0] and items[key][0] != items[key][1]


def _many(count: int, value: int) -> str:
    return "kind: strategy\nparams:\n" + "".join(f"  p{i:03d}: {value}\n" for i in range(count))


@pytest.mark.parametrize("delta,omitted", [(-1, 0), (0, 0), (1, 1)])
def test_ac5_flatten_item_count_boundary(ops_env, delta, omitted):
    count = L.FLATTEN_MAX_ITEMS + delta
    approval_id, _, _ = _pending_update(ops_env, old_cfg=_many(count, 1),
                                        new_cfg=_many(count, 2))
    params = _detail(ops_env, approval_id)["change"]["params"]
    assert len(params["items"]) == min(count, L.FLATTEN_MAX_ITEMS)
    # 旧・新の両側で超えた分が件数になる
    assert params["flatten_omitted"] == 2 * omitted


@pytest.mark.parametrize("added,shown,omitted", [(31, 63, 0), (32, 64, 0), (33, 64, 1)])
def test_ac5_params_diff_count_boundary(ops_env, added, shown, omitted):
    # 旧 32 項目が消え、新が added 項目増える: 差分は 32 + added 件 (63 / 64 / 65)
    old = "kind: strategy\nparams:\n" + "".join(f"  a{i:03d}: 1\n" for i in range(32))
    new = "kind: strategy\nparams:\n" + "".join(f"  b{i:03d}: 1\n" for i in range(added))
    assert 32 + added in (L.PARAMS_DIFF_MAX - 1, L.PARAMS_DIFF_MAX, L.PARAMS_DIFF_MAX + 1)
    approval_id, _, _ = _pending_update(ops_env, old_cfg=old, new_cfg=new)
    params = _detail(ops_env, approval_id)["change"]["params"]
    assert len(params["items"]) == shown and params["omitted"] == omitted


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_ac5_single_value_over_redaction_limit_is_hidden_alone(ops_env, delta):
    size = L.MAX_REDACT_VALUE_BYTES + delta
    big = "x" * size
    old = f"kind: strategy\nparams:\n  note: '{big}'\n  period: 14\n"
    new = f"kind: strategy\nparams:\n  note: '{big}y'\n  period: 15\n"
    approval_id, _, _ = _pending_update(ops_env, old_cfg=old, new_cfg=new)
    items = _items(_detail(ops_env, approval_id)["change"])
    assert items["params.period"] == ("14", "15")
    old_note, new_note = items["params.note"]
    if delta < 0:
        assert (old_note, new_note) == (big, big + "y")
    elif delta == 0:
        assert old_note == big and new_note == L.REDACTED
    else:
        assert (old_note, new_note) == (L.REDACTED, L.REDACTED)


def test_ac5_credential_named_param_values_are_hidden(ops_env):
    approval_id, _, _ = _pending_update(
        ops_env, old_cfg="kind: strategy\nparams:\n  api_token: aaa\n  period: 1\n",
        new_cfg="kind: strategy\nparams:\n  api_token: bbb\n  period: 2\n")
    items = _items(_detail(ops_env, approval_id)["change"])
    assert items["params.api_token"] == (L.REDACTED, L.REDACTED)
    assert items["params.period"] == ("1", "2")


def test_ac5_yaml_with_aliases_or_garbage_gives_no_params_diff(ops_env):
    bomb = "a: &a [1,2,3]\nparams:\n  x: *a\n"
    approval_id, _, _ = _pending_update(ops_env, old_cfg=OLD_CFG, new_cfg=bomb)
    params = _detail(ops_env, approval_id)["change"]["params"]
    assert params["items"] == [] and params["reason"] == L.REDACTED_CODE
    approval_id, _, _ = _pending_update(ops_env, old_cfg="a: [unclosed\n", new_cfg=OLD_CFG)
    assert _detail(ops_env, approval_id)["change"]["params"]["reason"] == L.REDACTED_CODE


# ---------------------------------------------------------------- AC-6 / 31a

def test_ac6_missing_sources_return_unavailable_with_200_shape(ops_env):
    digest = hash_of(PLUGIN_PY, CONFIG, TEST_PY)
    for status in ("pending", "approved", "rejected"):
        approval_id = insert_approval(ops_env, approval_payload(NAME, digest=digest),
                                      status=status)
        change = _detail(ops_env, approval_id)["change"]
        assert change["reason"] == L.UNAVAILABLE and L.REASON_TEXT[L.UNAVAILABLE] == \
            "変更内容を取得できません"
        body = _change(ops_env, approval_id)
        assert body["change"]["reason"] == L.UNAVAILABLE and body["diff"]["files"] == []


def test_ac6_rejected_reads_only_the_exact_archive_not_a_content_hash_twin(ops_env):
    plugins = ops_env.plugins_root
    # 同じ content (plugin.py + config.yaml) で test だけ違う archive は別の artifact_hash
    twin = make_archive(plugins, 7, test="def test_x():\n    assert 1\n")
    wanted = hash_of(PLUGIN_PY, CONFIG, TEST_PY)
    assert twin != wanted
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=wanted),
                                  status="rejected")
    assert _detail(ops_env, approval_id)["change"]["reason"] == L.UNAVAILABLE
    make_archive(plugins, 7)
    change = _detail(ops_env, approval_id)["change"]
    assert change["reason"] is None and change["candidate_hash"] == wanted


def test_ac6_rejected_candidate_without_journal_has_unknown_baseline_but_reads_archive(ops_env):
    plugins = ops_env.plugins_root
    digest = make_archive(plugins, 7, config=NEW_CFG)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=digest),
                                  status="rejected")
    change = _detail(ops_env, approval_id)["change"]
    assert change["classification"] == L.CLASS_UNKNOWN and change["reason"] is None


def test_ac6_revocation_style_payload_without_candidate_fields_is_unavailable(ops_env):
    approval_id = insert_approval(
        ops_env, {"name": NAME, "content_hash": "a" * 64, "artifact_hash": "b" * 64,
                  "revokes_approval_id": 1}, status="rejected", reason="revoked_by_operator")
    change = _detail(ops_env, approval_id)["change"]
    assert change["reason"] == L.UNAVAILABLE


def test_ac6_service_without_plugins_root_reports_unavailable(ops_env):
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest="a" * 64))
    service = ops_env.service(plugins_root=None)
    assert service.approval_detail(OPERATOR, approval_id)["change"]["reason"] == L.UNAVAILABLE


def test_ac31a_too_large_is_change_wide(ops_env):
    big = "#" * L.MAX_FILE_BYTES + "\n"
    plugins = ops_env.plugins_root
    old_hash = make_version(plugins, NAME, config=OLD_CFG)
    make_live(plugins, NAME, old_hash)
    new_hash = make_staging(plugins, 7, NAME, plugin_py=big, config=NEW_CFG)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=new_hash))
    body = _change(ops_env, approval_id)
    assert body["change"]["reason"] == L.TOO_LARGE
    assert body["change"]["params"]["items"] == [] and body["diff"]["files"] == []
    assert body["change"]["classification"] == L.CLASS_UNKNOWN


def test_ac31a_hash_mismatch_is_change_wide_and_returns_no_bytes(ops_env):
    plugins = ops_env.plugins_root
    new_hash = make_staging(plugins, 7, NAME, config=NEW_CFG)
    (plugins / "_staging" / "7" / NAME / "plugin.py").write_text("TAMPER_MARK = 1\n")
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=new_hash))
    body = _change(ops_env, approval_id)
    assert body["change"]["reason"] == L.HASH_MISMATCH and body["diff"]["files"] == []
    assert "TAMPER_MARK" not in json.dumps(body)


def test_ac31a_unsafe_source_is_change_wide(ops_env):
    plugins = ops_env.plugins_root
    new_hash = make_staging(plugins, 7, NAME, config=NEW_CFG)
    approval_id = insert_approval(ops_env, approval_payload(
        NAME, digest=new_hash, candidate_path="plugins/_staging/7/../7/" + NAME))
    body = _change(ops_env, approval_id)
    assert body["change"]["reason"] == L.UNSAFE_SOURCE and body["diff"]["files"] == []


def test_ac31a_non_utf8_file_is_hidden_alone_and_other_files_are_shown(ops_env):
    plugins = ops_env.plugins_root
    old_hash = make_version(plugins, NAME, config=OLD_CFG)
    make_live(plugins, NAME, old_hash)
    new_hash = make_staging(plugins, 7, NAME, config=NEW_CFG, test=b"def test_x():\n\xff\xfe\n")
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=new_hash))
    files = {f["path"]: f for f in _change(ops_env, approval_id)["diff"]["files"]}
    assert files["test_plugin.py"]["status"] == "redacted"
    assert files["test_plugin.py"]["reason"] == L.REDACTED_CODE
    assert files["test_plugin.py"]["hunks"] == []
    assert files["config.yaml"]["status"] == "changed" and files["config.yaml"]["hunks"]
    assert files["plugin.py"]["status"] == "unchanged"


# ---------------------------------------------------------------- AC-24 human origin / archive_path

def test_ac24_human_origin_candidate_is_read_and_bad_locators_are_unsafe(ops_env):
    plugins = ops_env.plugins_root
    digest = make_human(plugins, NAME, config=NEW_CFG)
    ok = insert_approval(ops_env, approval_payload(NAME, digest=digest, origin="human"))
    change = _detail(ops_env, ok)["change"]
    assert change["reason"] is None and change["candidate_hash"] == digest
    assert change["classification"] == L.CLASS_NEW
    bad_locator = insert_approval(ops_env, approval_payload(
        NAME, digest=digest, origin="human", candidate_path="plugins/_human/other"))
    assert _detail(ops_env, bad_locator)["change"]["reason"] == L.UNSAFE_SOURCE
    unknown = insert_approval(ops_env, approval_payload(
        NAME, digest=digest, origin="human", candidate_origin="mystery"))
    assert _detail(ops_env, unknown)["change"]["reason"] == L.UNSAFE_SOURCE


def test_ac24_broken_archive_path_in_payload_is_never_used(ops_env, tmp_path):
    plugins = ops_env.plugins_root
    digest = make_archive(plugins, 7, config=NEW_CFG)
    outside = tmp_path / "outside"
    write_files(outside, config="SECRET_OUTSIDE: 1\n")
    approval_id = insert_approval(
        ops_env, approval_payload(NAME, digest=digest, archive_path=str(outside),
                                  candidate_path=str(outside)), status="rejected")
    body = _change(ops_env, approval_id)
    assert body["change"]["reason"] is None
    assert "SECRET_OUTSIDE" not in json.dumps(body)


def test_ac24_intermediate_symlink_in_staging_is_unsafe_end_to_end(ops_env, tmp_path):
    plugins = ops_env.plugins_root
    outside = tmp_path / "outside"
    digest = write_files(outside / NAME, config=NEW_CFG)
    (plugins / "_staging").mkdir(parents=True)
    os.symlink(outside, plugins / "_staging" / "7")
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=digest))
    assert _change(ops_env, approval_id)["change"]["reason"] == L.UNSAFE_SOURCE


def test_ac24_no_resolved_path_in_response_or_logs(ops_env, tmp_path, caplog):
    plugins = ops_env.plugins_root
    digest = make_staging(plugins, 7, NAME, config=NEW_CFG)
    (plugins / "_staging" / "7" / NAME / "plugin.py").write_text("x = 1\n")
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=digest))
    with caplog.at_level(logging.DEBUG):
        body = _change(ops_env, approval_id)
        detail = _detail(ops_env, approval_id)
    text = json.dumps(body) + json.dumps(detail, default=str) + caplog.text
    assert str(tmp_path) not in text and str(plugins) not in text


def test_ac24_computation_failure_is_a_fixed_code_without_exception_text(ops_env, monkeypatch,
                                                                         caplog):
    approval_id, _, _ = _pending_update(ops_env)

    def boom(**kwargs):
        raise RuntimeError("/secret/path/should/not/leak")
    monkeypatch.setattr(change_mod, "compute_change", boom)
    with caplog.at_level(logging.DEBUG):
        body = _change(ops_env, approval_id)
    assert body["change"]["reason"] == L.UNSAFE_SOURCE
    assert "secret" not in json.dumps(body) + caplog.text


# ---------------------------------------------------------------- 構造化 diff と 512 KiB

def test_ac14_diff_is_structured_unescaped_and_redacted(ops_env):
    old_py = "x = 1\n"
    new_py = "x = 1\ny = a & b < c > d  # 'q' \"z\"\n"
    plugins = ops_env.plugins_root
    old_hash = make_version(plugins, NAME, plugin_py=old_py, config=OLD_CFG)
    make_live(plugins, NAME, old_hash)
    new_hash = make_staging(plugins, 7, NAME, plugin_py=new_py, config=OLD_CFG)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=new_hash))
    body = _change(ops_env, approval_id)
    plugin = next(f for f in body["diff"]["files"] if f["path"] == "plugin.py")
    assert plugin["status"] == "changed"
    assert plugin["hunks"][0]["header"] == "@@ -1 +1,2 @@"
    assert plugin["hunks"][0]["lines"] == [" x = 1", "+y = a & b < c > d  # 'q' \"z\""]
    raw = json.dumps(body, ensure_ascii=False)
    assert "&amp;" not in raw and "&lt;" not in raw and "a & b < c" in raw


def test_ac14_new_candidate_is_all_added_lines(ops_env):
    digest = make_staging(ops_env.plugins_root, 7, NAME)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=digest))
    files = {f["path"]: f for f in _change(ops_env, approval_id)["diff"]["files"]}
    assert files["plugin.py"]["status"] == "added"
    lines = files["plugin.py"]["hunks"][0]["lines"]
    assert lines == ["+" + line for line in PLUGIN_PY.splitlines()]
    assert files["plugin.py"]["hunks"][0]["header"] == f"@@ -0,0 +1,{len(lines)} @@"


def _counted(files):
    return sum(len(h["header"].encode()) + 1 + sum(len(x.encode()) + 1 for x in h["lines"])
               for f in files for h in f["hunks"])


def _fake_files(sizes):
    files = []
    for index, size in enumerate(sizes):
        files.append({"path": f"f{index}", "status": "added", "reason": None,
                      "hunks": [{"header": "@@ -0,0 +1,3 @@",
                                 "lines": ["+" + "a" * size for _ in range(3)]}],
                      "omitted_lines": 0})
    return files


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_ac14_api_diff_budget_boundary_is_applied_by_line(delta):
    files = _fake_files([100, 100, 100])
    full = _counted(files)
    budget = full + delta
    omitted = change_mod.apply_diff_budget(files, budget)
    assert _counted(files) <= budget
    if delta >= 0:
        assert omitted == 0 and _counted(files) == full
    else:
        assert omitted == 1
        assert files[-1]["omitted_lines"] == 1 and len(files[-1]["hunks"][0]["lines"]) == 2


def test_ac14_api_diff_budget_uses_the_512_kib_constant_end_to_end(ops_env):
    line = "#" * 1000 + "\n"
    big = line * 250  # 約 250 KiB (読み取り上限未満) を 3 ファイルに置く
    digest = make_staging(ops_env.plugins_root, 7, NAME, plugin_py=big, config=big, test=big)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=digest))
    body = _change(ops_env, approval_id)
    assert _counted(body["diff"]["files"]) <= L.API_DIFF_BYTES
    assert body["diff"]["omitted_lines"] > 0
    assert sum(f["omitted_lines"] for f in body["diff"]["files"]) == body["diff"]["omitted_lines"]
    first = body["diff"]["files"][0]
    assert first["omitted_lines"] == 0 and body["diff"]["files"][-1]["omitted_lines"] > 0


# ---------------------------------------------------------------- AC-22a / 23a (API)

def test_ac22a_adversarial_fixture_never_leaves_the_api(ops_env):
    plugins = ops_env.plugins_root
    old_hash = make_version(plugins, NAME, plugin_py="x = 1\n", config=OLD_CFG)
    make_live(plugins, NAME, old_hash)
    cfg = NEW_CFG + f"  note: 'see {ADVERSARIAL_SECRETS[0]} {ADVERSARIAL_SECRETS[5]}'\n"
    digest = make_staging(plugins, 7, NAME, plugin_py=ADVERSARIAL_PLUGIN, config=cfg)
    payload = approval_payload(NAME, digest=digest)
    payload["agent_claims"]["summary"] = "data at " + ADVERSARIAL_SECRETS[0] + " " + \
        ADVERSARIAL_SECRETS[6] + " " + ADVERSARIAL_SECRETS[2]
    approval_id = insert_approval(ops_env, payload)
    for secret in ADVERSARIAL_SECRETS:
        assert secret in ADVERSARIAL_PLUGIN
    text = json.dumps(_change(ops_env, approval_id), ensure_ascii=False) + \
        json.dumps(_detail(ops_env, approval_id)["change"], ensure_ascii=False)
    for secret in ADVERSARIAL_SECRETS:
        assert secret not in text, secret
    assert L.REDACTED in text


def test_ac22a_undeterminable_file_hidden_whole_other_files_shown(ops_env):
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY-----\n"
    digest = make_staging(ops_env.plugins_root, 7, NAME, plugin_py="x = 1\n", test=TEST_PY + key)
    approval_id = insert_approval(ops_env, approval_payload(NAME, digest=digest))
    body = _change(ops_env, approval_id)
    files = {f["path"]: f for f in body["diff"]["files"]}
    assert files["test_plugin.py"]["status"] == "redacted"
    assert files["plugin.py"]["hunks"] and "MIIabc" not in json.dumps(body)


def test_ac23a_redaction_runs_before_any_cut_so_a_trailing_token_cannot_survive(ops_env):
    token = "ghp_Zx81QwErTyUiOpAsDfGhJkLzXcVbNm0123"
    value = "&" * 70 + " " + token  # 80 字の手前に escape で膨らむ記号 + 末尾に token
    old = "kind: strategy\nparams:\n  note: 'old'\n"
    new = f"kind: strategy\nparams:\n  note: '{value}'\n"
    approval_id, _, _ = _pending_update(ops_env, old_cfg=old, new_cfg=new)
    items = _items(_detail(ops_env, approval_id)["change"])
    assert token not in items["params.note"][1]
    assert items["params.note"][1] == "&" * 70 + " " + L.REDACTED
