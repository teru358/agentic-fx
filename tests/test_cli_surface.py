"""The CLI surface metadata stays inert until the help integration task."""
from __future__ import annotations

import dataclasses
import importlib.metadata
import subprocess
import sys

import pytest

from agentic_fx import cli_surface
from agentic_fx.cli_surface import Group as G


def test_command_records_cover_the_seven_groups_and_publish_implemented_commands():
    from agentic_fx import cli_surface

    assert tuple(group.value for group in cli_surface.Group) == (
        "導入・接続管理", "稼働観測・実行追跡", "市場入力を整える", "調査・評価する",
        "問い合わせ・改善運用", "Plugin を採否・配備する", "保護状態を回復する")
    assert {record.group for record in cli_surface.COMMANDS if record.classified} == set(
        cli_surface.Group)
    assert all(record.exposed for record in cli_surface.COMMANDS
               if record.implemented and not record.planned)
    assert all(record.implemented for record in cli_surface.COMMANDS if record.exposed)
    assert all(not record.exposed for record in cli_surface.COMMANDS if record.planned)


def test_compatibility_expiry_uses_integer_version_tuples():
    from agentic_fx import cli_surface

    assert cli_surface.version_tuple("0.10.0") > cli_surface.version_tuple("0.3.0")
    assert {record.remove_in for record in cli_surface.COMPATIBILITY_RECORDS} == {"0.3.0"}
    cli_surface.validate_compatibility_versions("0.2.999")
    with pytest.raises(ValueError, match="expired compatibility record"):
        cli_surface.validate_compatibility_versions("0.3.0")


def test_shell_stop_is_an_unclassified_change_record():
    from agentic_fx import cli_surface

    stop = next(record for record in cli_surface.COMMANDS if record.command == "stop")
    assert (stop.classified, stop.shell, stop.label, stop.shell_usage) == (
        False, True, cli_surface.Label.CHANGE, "stop")


def test_cli_surface_import_has_no_agentic_fx_import_closure():
    code = (
        "import sys, agentic_fx.cli_surface; "
        "loaded = sorted(name for name in sys.modules "
        "if name == 'agentic_fx' or name.startswith('agentic_fx.')); "
        "print(','.join(loaded)); "
        "raise SystemExit(0 if loaded == ['agentic_fx', 'agentic_fx.cli_surface'] else 1)"
    )
    completed = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stdout + completed.stderr


# Hand-written expectation of the completed command table: (command, group, lane, usage,
# shell_usage, label, confirm, approver, scope, endpoint_code, daemon_required, condition).
EXPECTED = (
    ('init', G.SETUP, "LOCAL", 'afx init [--non-interactive] [--config FILE]', None, "CHANGE", False, False, None, None, False, None),
    ('check', G.SETUP, "LOCAL", 'afx check [--json] [--unit-env]', None, "READ", False, False, None, None, False, None),
    ('check backend', G.SETUP, "LOCAL", 'afx check backend --backend <local|claude|codex|opencode> [--provider chatgpt]', None, "CHANGE", False, False, None, None, False, None),
    ('keys init', G.SETUP, "LOCAL", 'afx keys init', None, "CHANGE", False, False, None, None, False, '停止中のみ'),
    ('keys enroll', G.SETUP, "LOCAL", 'afx keys enroll <principal>', None, "CHANGE", False, False, None, None, False, '停止中のみ'),
    ('keys rotate', G.SETUP, "LOCAL", 'afx keys rotate <principal>', None, "CHANGE", False, False, None, None, False, '停止中のみ'),
    ('keys revoke', G.SETUP, "LOCAL", 'afx keys revoke <principal>', None, "CHANGE", False, False, None, None, False, '停止中のみ'),
    ('ops enable', G.SETUP, "LOCAL", 'afx ops <bot|web|notify> enable', None, "CHANGE", False, False, None, None, False, '必要時だけ daemon を一時停止して再開'),
    ('ops disable', G.SETUP, "LOCAL", 'afx ops <bot|web|notify> disable', None, "CHANGE", False, False, None, None, False, None),
    ('ops web reconfigure', G.SETUP, "LOCAL", 'afx ops web reconfigure', None, "CHANGE", False, False, None, None, False, None),
    ('ops bot reconfigure', G.SETUP, "LOCAL", 'afx ops bot reconfigure', None, "CHANGE", False, False, None, None, False, None),
    ('ops notify reconfigure', G.SETUP, "LOCAL", 'afx ops notify reconfigure', None, "CHANGE", False, False, None, None, False, None),
    ('ops status', G.SETUP, "LOCAL", 'afx ops <bot|web|notify> status', None, "READ", False, False, None, None, False, None),
    ('completion bash', G.SETUP, "LOCAL", 'afx completion bash', None, "READ", False, False, None, None, False, None),
    ('ctl whoami', G.SETUP, "CTL", 'afx ctl whoami', None, "READ", False, False, 'status.read', 'whoami', True, None),
    ('status', G.OBSERVE, "LOCAL", 'afx status', None, "READ", False, False, None, None, False, None),
    ('ctl status', G.OBSERVE, "CTL", 'afx ctl status', 'status', "READ", False, False, 'status.read', 'status', True, None),
    ('ctl log', G.OBSERVE, "CTL", 'afx ctl log [n]', 'log [n]', "READ", False, False, 'logs.read', 'log', True, None),
    ('ctl activity', G.OBSERVE, "CTL", 'afx ctl activity [n] [category]', 'activity [n] [category]', "READ", False, False, 'logs.read', 'activity', True, None),
    ('ctl events', G.OBSERVE, "CTL", 'afx ctl events [--after <id>] [--limit <n>] [--follow]', None, "READ", False, False, 'events.read', 'events', True, None),
    ('ctl jobs get', G.OBSERVE, "CTL", 'afx ctl jobs get <id>', None, "READ", False, False, 'jobs.own', 'job_get', True, None),
    ('ctl jobs cancel', G.OBSERVE, "CTL", 'afx ctl jobs cancel <id>', None, "CHANGE", True, False, 'jobs.own', 'job_cancel', True, None),
    ('news list', G.MARKET, "LOCAL", 'afx news list', None, "READ", False, False, None, None, False, None),
    ('news test', G.MARKET, "LOCAL", 'afx news test <name>', None, "READ", False, False, None, None, False, None),
    ('news add', G.MARKET, "LOCAL", 'afx news add <name> <url> [--fetcher feed|web] [--yes]', None, "CHANGE", False, False, None, None, False, '0 件のとき確認'),
    ('news enable', G.MARKET, "LOCAL", 'afx news enable <name>', None, "CHANGE", False, False, None, None, False, '次の収集周期で反映'),
    ('news disable', G.MARKET, "LOCAL", 'afx news disable <name>', None, "CHANGE", False, False, None, None, False, '次の収集周期で反映'),
    ('history import', G.MARKET, "LOCAL", 'afx history import …', None, "CHANGE", False, False, None, None, False, None),
    ('history compare', G.MARKET, "LOCAL", 'afx history compare …', None, "READ", False, False, None, None, False, None),
    ('history coverage', G.MARKET, "LOCAL", 'afx history coverage …', None, "READ", False, False, None, None, False, None),
    ('backtest run', G.RESEARCH, "LOCAL", 'afx backtest run …', None, "CHANGE", False, False, None, None, False, None),
    ('analyze corr', G.RESEARCH, "LOCAL", 'afx analyze corr …', None, "CHANGE", False, False, None, None, False, None),
    ('ctl ask', G.GUIDANCE, "CTL", 'afx ctl ask <question>', 'ask <質問>', "CHANGE", False, False, 'operate', 'ask', True, None),
    ('ctl improve', G.GUIDANCE, "CTL", 'afx ctl improve', 'improve', "CHANGE", False, False, 'operate', 'improve', True, None),
    ('ctl backlog add', G.GUIDANCE, "CTL", 'afx ctl backlog add <idea>', 'backlog add <idea>', "CHANGE", False, False, 'operate', 'backlog_add', True, None),
    ('ctl backlog list', G.GUIDANCE, "CTL", 'afx ctl backlog list [--all] [--limit <n>]', None, "READ", False, False, 'guidance.read', 'backlog_list', True, None),
    ('ctl backlog reject', G.GUIDANCE, "CTL", 'afx ctl backlog reject <id>', 'backlog reject <id>', "CHANGE", False, False, 'operate', 'backlog_reject', True, None),
    ('ctl backlog reopen', G.GUIDANCE, "CTL", 'afx ctl backlog reopen <id>', 'backlog reopen <id>', "CHANGE", False, False, 'operate', 'backlog_reopen', True, None),
    ('ctl backlog note', G.GUIDANCE, "CTL", 'afx ctl backlog note <id>', 'backlog note <id>', "CHANGE", False, False, 'operate', 'backlog_note', True, None),
    ('ctl policy add', G.GUIDANCE, "CTL", 'afx ctl policy add <text>', 'policy add <text>', "CHANGE", False, False, 'operate', 'policy', True, None),
    ('ctl policy show', G.GUIDANCE, "CTL", 'afx ctl policy show', None, "READ", False, False, 'guidance.read', 'policy_show', True, None),
    ('ctl policy remove', G.GUIDANCE, "CTL", 'afx ctl policy remove <id>', None, "CHANGE", False, False, 'operate', 'policy_remove', True, None),
    ('ctl policy reconcile', G.GUIDANCE, "CTL", 'afx ctl policy reconcile', None, "CHANGE", False, False, 'operate', 'policy_reconcile', True, None),
    ('ctl reflect retry', G.GUIDANCE, "CTL", 'afx ctl reflect retry <order_id>', 'reflect retry <order_id>', "CHANGE", False, False, 'operate', 'reflection_retry', True, None),
    ('plugin submit', G.PLUGIN, "LOCAL", 'afx plugin submit <name> --from _human', None, "CHANGE", False, False, None, None, False, None),
    ('plugin bless', G.PLUGIN, "LOCAL", 'afx plugin bless <name> --from _human', None, "CHANGE", False, False, None, None, False, None),
    ('plugin materialize', G.PLUGIN, "LOCAL", 'afx plugin materialize <name>', None, "CHANGE", False, False, None, None, False, None),
    ('plugin lock', G.PLUGIN, "LOCAL", 'afx plugin lock <name> --from _human', None, "CHANGE", False, False, None, None, False, None),
    ('plugin retire', G.PLUGIN, "LOCAL", 'afx plugin retire <name> …', None, "CHANGE", True, False, None, None, False, '運用時間外'),
    ('plugin disable', G.PLUGIN, "LOCAL", 'afx plugin disable <name> …', None, "CHANGE", True, False, None, None, False, '再起動後反映'),
    ('plugin rollback', G.PLUGIN, "LOCAL", 'afx plugin rollback <name> …', None, "CHANGE", True, False, None, None, False, '再起動後反映'),
    ('plugin revoke', G.PLUGIN, "LOCAL", 'afx plugin revoke <name> --version HASH …', None, "CHANGE", True, False, None, None, False, None),
    ('plugin versions', G.PLUGIN, "LOCAL", 'afx plugin versions <name>', None, "READ", False, False, None, None, False, None),
    ('plugin list', G.PLUGIN, "LOCAL", 'afx plugin list [--kind indicator|strategy]', None, "READ", False, False, None, None, False, None),
    ('ctl approval list', G.PLUGIN, "CTL", 'afx ctl approval list [n]', 'approval list [n]', "READ", False, False, 'approvals.list', 'approval_list', True, None),
    ('ctl approval show', G.PLUGIN, "CTL", 'afx ctl approval show <id>', 'approval show <id>', "READ", False, False, 'approvals.detail', 'approval_detail', True, None),
    ('ctl approve', G.PLUGIN, "CTL", 'afx ctl approve <id>', 'approve <id>', "CHANGE", True, True, 'decide', 'approve', True, None),
    ('ctl reject', G.PLUGIN, "CTL", 'afx ctl reject <id> [reason]', 'reject <id> [理由]', "CHANGE", True, True, 'decide', 'reject', True, None),
    ('ctl approval retry', G.PLUGIN, "CTL", 'afx ctl approval retry <id>', 'approval retry <id>', "CHANGE", True, True, 'decide', 'retry', True, None),
    ('ctl killswitch reset', G.RECOVERY, "CTL", 'afx ctl killswitch reset', 'killswitch reset', "READ", False, False, 'status.read', 'status', True, None),
    ('ctl killswitch reset', G.RECOVERY, "CTL", 'afx ctl killswitch reset <generation>', 'killswitch reset <世代>', "CHANGE", True, True, 'local_guard', 'killswitch_reset', True, None),
    ('ctl killswitch reconcile', G.RECOVERY, "CTL", 'afx ctl killswitch reconcile [confirm]', 'killswitch reconcile [confirm]', "CHANGE", True, True, 'local_guard', 'killswitch_reconcile', True, None),
    ('ctl data resume', G.RECOVERY, "CTL", 'afx ctl data resume [--acknowledge]', 'data resume [--acknowledge]', "CHANGE", True, True, 'local_guard', 'data_resume', True, None),
    ('stop', None, "CTL", 'afx stop', 'stop', "CHANGE", False, False, None, None, False, None),
)


def _all_exposed():
    return tuple(
        record if record.planned else dataclasses.replace(record, exposed=True, implemented=True)
        for record in cli_surface.COMMANDS)


def _renderer():
    return cli_surface.SurfaceRenderer(_all_exposed())


def _record(command):
    return next(record for record in cli_surface.COMMANDS if record.command == command)


def test_every_record_matches_the_independent_expected_table():
    assert [record.command for record in cli_surface.COMMANDS] == [row[0] for row in EXPECTED]
    for record, row in zip(cli_surface.COMMANDS, EXPECTED, strict=True):
        (command, group, lane, usage, shell_usage, label, confirm, approver, scope, endpoint,
         daemon, condition) = row
        assert (record.group, record.lane.name, record.usage, record.shell_usage,
                record.label.name, record.confirm, record.approver, record.scope,
                record.endpoint_code, record.daemon_required, record.condition) == (
            group, lane, usage, shell_usage, label, confirm, approver, scope, endpoint,
            daemon, condition), command
        assert record.mutating == (label != "READ"), command
        assert record.shell == (shell_usage is not None), command
        assert (record.group_order is None) if group is None else (
            record.group_order == tuple(G).index(group) + 1), command


def test_record_summaries_carry_the_table_notes():
    assert _record("ops enable").condition == "必要時だけ daemon を一時停止して再開"
    for feature in ("web", "bot", "notify"):
        assert "afx に触れず" in _record(f"ops {feature} reconfigure").summary
        assert _record(f"ops {feature} reconfigure").aliases == ("reconf",)
    web, bot, notify = (_record(f"ops {name} reconfigure") for name in ("web", "bot", "notify"))
    assert (bot.implemented, bot.exposed, notify.implemented, notify.exposed) == (
        False, False, False, False)
    assert (web.implemented, web.exposed) == (True, True)
    assert [_record(f"news {action}").label for action in ("list", "test")] == [
        cli_surface.Label.READ, cli_surface.Label.READ]
    assert [_record(f"news {action}").label for action in ("add", "enable", "disable")] == [
        cli_surface.Label.CHANGE, cli_surface.Label.CHANGE, cli_surface.Label.CHANGE]


def test_endpoint_codes_and_scopes_agree_with_the_service_table():
    from agentic_fx.ops.service import ENDPOINTS

    for record in cli_surface.COMMANDS:
        code = record.endpoint_code
        if code is None:
            assert record.scope is None, record.usage
            continue
        if code not in ENDPOINTS:
            assert not record.implemented, record.usage  # endpoint arrives with its record
            continue
        assert ENDPOINTS[code].scope.value == record.scope, record.usage
        assert ENDPOINTS[code].mutating == record.mutating, record.usage


def test_killswitch_read_and_change_forms_share_one_parser_node():
    reset = [r for r in cli_surface.COMMANDS if r.command == "ctl killswitch reset"]
    assert [(r.usage, r.label.name, r.endpoint_code) for r in reset] == [
        ("afx ctl killswitch reset", "READ", "status"),
        ("afx ctl killswitch reset <generation>", "CHANGE", "killswitch_reset")]
    assert not any("generation" in r.command for r in cli_surface.COMMANDS)
    r = _renderer()
    assert r.parser_help("ctl killswitch reset") == (
        "[下位操作ごと] kill switch の世代を表示する / kill switch を解除する")
    assert r.leaf_help("afx ctl killswitch reset") == "[読取] kill switch の世代を表示する"
    assert r.leaf_help("afx ctl killswitch reset <generation>") == (
        "[変更] [確認] [approver] kill switch を解除する")
    epilog = r.render_epilog("ctl killswitch")
    assert epilog.count("afx ctl killswitch reset  ") == 1
    assert epilog.count("afx ctl killswitch reset <generation>  ") == 1


def test_every_node_with_children_has_parser_help():
    r = _renderer()
    nodes = set()
    for record in _all_exposed():
        if record.planned:
            continue
        paths = ([f"ops {f} {record.command.split(' ', 1)[1]}" for f in ("bot", "web", "notify")]
                 if record.command in cli_surface._OPS_GENERIC else [record.command])
        for path in paths:
            words = path.split(" ")
            nodes.update(" ".join(words[:size]) for size in range(1, len(words)))
    assert {"ctl jobs", "ctl backlog", "ctl policy", "ctl approval", "ctl killswitch",
            "ctl data", "ctl reflect", "ops web"} <= nodes
    for node in sorted(nodes):
        assert r.parser_help(node), node
        if node.startswith("ops ") and node.count(" ") == 1:
            assert r.parser_description(node) is None
        else:
            assert r.parser_description(node), node
        if node.startswith("ops ") and node.count(" ") == 1:
            assert r.render_epilog(node) == ""
        else:
            assert r.render_epilog(node), node


def test_alias_and_compatibility_text_is_pinned():
    assert [(a.usage, a.canonical_usage, a.parser_alias) for a in cli_surface.ALIASES] == [
        ("afx ctl improve add <idea>", "afx ctl backlog add <idea>", "improve add"),
        ("afx ops web reconf", "afx ops web reconfigure", "reconf"),
        ("afx ops bot reconf", "afx ops bot reconfigure", "reconf"),
        ("afx ops notify reconf", "afx ops notify reconfigure", "reconf"),
        ("afx ctl approvals list [n]", "afx ctl approval list [n]", "approvals list"),
        ("afx ctl approvals show <id>", "afx ctl approval show <id>", "approvals show"),
        ("afx ctl approval <id>", "afx ctl approval show <id>", "approval <id>"),
        ("afx ctl retry <id>", "afx ctl approval retry <id>", "retry <id>"),
    ]
    improve, ops = cli_surface.COMPATIBILITY_RECORDS
    assert (improve.usage, improve.canonical_usage, improve.deprecated_since, improve.remove_in,
            improve.rejects, improve.message) == (
        "afx improve …", "afx check backend --backend <name> [--provider chatgpt]", "0.2.0",
        "0.3.0", True,
        "afx improve verify-backend は廃止されました。afx check backend --backend <name> "
        "[--provider chatgpt] を使ってください。")
    assert (ops.usage, ops.canonical_usage, ops.deprecated_since, ops.remove_in, ops.rejects,
            ops.message) == (
        "afx ops <enable|disable|status> <bot|web|notify> …",
        "afx ops <bot|web|notify> <enable|disable|status> …", "0.2.0", "0.3.0", False,
        "afx ops <verb> <feature> は旧形です。afx ops <feature> <verb> を使ってください。"
        "この形は 0.3.0 で削除します。")


def test_record_aliases_are_derived_from_the_alias_table_uniquely():
    keys = [(a.canonical_usage, a.parser_alias) for a in cli_surface.ALIASES]
    assert len(set(keys)) == len(keys)
    for alias in cli_surface.ALIASES:
        owners = [r for r in cli_surface.COMMANDS if r.usage == alias.canonical_usage]
        assert len(owners) == 1, alias.canonical_usage
        assert alias.parser_alias in owners[0].aliases
    assert sorted(alias for r in cli_surface.COMMANDS for alias in r.aliases) == sorted(
        a.parser_alias for a in cli_surface.ALIASES)


@pytest.mark.parametrize("mutation", [
    {"exposed": True, "implemented": False},
    {"exposed": True, "planned": True, "implemented": True},
    {"exposed": True, "planned": True, "implemented": False},
])
def test_invalid_visibility_combinations_are_rejected(mutation):
    base = _record("ctl status")
    broken = dataclasses.replace(base, **mutation)
    records = (broken,) + tuple(r for r in cli_surface.COMMANDS if r is not base)
    with pytest.raises(ValueError):
        cli_surface._validate_records(records)
    with pytest.raises(ValueError):
        cli_surface.SurfaceRenderer(records)


def test_lifecycle_and_classification_mutations_are_rejected():
    base = _record("stop")
    others = tuple(r for r in cli_surface.COMMANDS if r is not base)
    for mutation in ({"remove_in": None}, {"deprecated_since": None},
                     {"deprecated_since": "1.0.0", "remove_in": "1.0.0"},
                     {"group": G.SETUP, "group_order": 1}):
        with pytest.raises(ValueError):
            cli_surface._validate_records((dataclasses.replace(base, **mutation),) + others)


def test_package_version_is_below_every_removal_and_checked_by_the_validator():
    current = importlib.metadata.version("agentic-fx")
    cli_surface.validate_compatibility_versions(current)
    removals = {r.remove_in for r in cli_surface.COMPATIBILITY_RECORDS} | {
        r.remove_in for r in cli_surface.COMMANDS if r.remove_in}
    assert removals == {"0.3.0", "1.0.0"}
    for removal in removals:
        assert cli_surface.version_tuple(current) < cli_surface.version_tuple(removal)
    with pytest.raises(ValueError, match="expired compatibility record: shell"):
        cli_surface.validate_compatibility_versions("1.0.0", compatibility=())


def test_package_version_reaches_every_deprecated_since():
    current = cli_surface.version_tuple(importlib.metadata.version("agentic-fx"))
    since = {r.deprecated_since for r in cli_surface.COMPATIBILITY_RECORDS} | {
        r.deprecated_since for r in cli_surface.COMMANDS if r.deprecated_since}
    assert since == {"0.2.0"}
    assert all(current >= cli_surface.version_tuple(value) for value in since)


def test_version_expiry_pin_rejects_the_next_compatibility_release(monkeypatch):
    monkeypatch.setattr(importlib.metadata, "version", lambda _name: "0.3.0")
    with pytest.raises(ValueError, match="expired compatibility record"):
        cli_surface.validate_compatibility_versions(importlib.metadata.version("agentic-fx"))


def test_shell_lifecycle_has_one_owner():
    stop = _record("stop")
    assert (stop.deprecated_since, stop.remove_in) == (
        cli_surface.SHELL_LIFECYCLE.deprecated_since, cli_surface.SHELL_LIFECYCLE.remove_in)
    assert cli_surface.RENDERER.render_shell_help()
    text = _renderer().render_shell_help()
    assert text.splitlines()[-1] == (
        "廃止予定: 対話 shell は 0.2.0 で deprecated。1.0.0 で削除予定。afx ctl を使用してください。")
    assert "  stop  daemon を graceful shutdown する" in text.splitlines()
    moved = cli_surface.ShellLifecycle("0.2.0", "2.0.0")
    shifted = cli_surface.SurfaceRenderer(_all_exposed(), shell_lifecycle=moved).render_shell_help()
    assert "2.0.0 で削除予定" in shifted and "1.0.0" not in shifted
    without_stop = tuple(r for r in _all_exposed() if r.command != "stop")
    assert cli_surface.SurfaceRenderer(without_stop).render_shell_help().splitlines()[-1] == (
        text.splitlines()[-1])
    with pytest.raises(ValueError, match="expired compatibility record: shell"):
        cli_surface.validate_compatibility_versions(
            "2.0.0", compatibility=(), commands=(), shell_lifecycle=cli_surface.SHELL_LIFECYCLE)
    cli_surface.validate_compatibility_versions(
        "1.5.0", compatibility=(), commands=(), shell_lifecycle=moved)


def test_shell_advertises_the_canonical_backlog_add_form():
    assert _record("ctl backlog add").shell_usage == "backlog add <idea>"


def test_the_ctl_parser_accepts_the_advertised_backlog_add_form():
    """Metadata の正式 shell 形は parser が同じ backlog 操作へ正規化する。"""
    from agentic_fx.ops.client import parse
    op = parse(["backlog", "add", "x"])
    assert (op.name, op.args, op.via_alias) == ("backlog_add", ["x"], None)


def test_public_records_render_and_reserved_records_do_not():
    for name in ("top_description", "render_top_epilog"):
        assert getattr(cli_surface, name)()
    for command in ("ops", "ops web", "keys", "init", "ops web enable"):
        assert cli_surface.parser_help(command)
    for command in ("ops", "keys", "init", "ops web enable"):
        assert cli_surface.parser_description(command)
    assert cli_surface.parser_description("ops web") is None
    assert cli_surface.parser_help("news") == "[下位操作ごと] 市場ニュース源を扱う"
    assert cli_surface.parser_description("news") == "市場ニュース源を扱う"
    assert cli_surface.leaf_help("news") is None
    assert cli_surface.render_shell_help()


def test_parent_help_comes_from_the_parent_and_label_is_derived():
    r = _renderer()
    assert r.parser_help("ops") == "[下位操作ごと] bot / web / notify の有効化・無効化・再設定・状態表示"
    assert r.parser_description("ops") == "bot / web / notify の有効化・無効化・再設定・状態表示"
    assert r.parser_help("check") == (
        "[下位操作ごと] 設定と実行環境の検査 (`backend` は非隔離・課金ありの接続確認)")
    assert r.parser_help("history") == "[下位操作ごと] 履歴データ操作"
    assert r.parser_help("plugin") == "[下位操作ごと] plugin の採否・配備・表示"
    assert r.parser_help("keys") == "[変更] 操作 API の鍵管理 (停止中のみ)"
    assert r.parser_help("backtest") == "[変更] バックテスト"
    assert r.parser_help("ctl") == "稼働中の daemon を操作 API で操作する (afx ctl help)"
    assert r.parser_help("ops web") == "web 表示 (ops-web)"
    assert r.parser_help("init") == "[変更] 初期設定 (対話。`--non-interactive` で従来の非対話)"
    assert r.parser_help("status") == "[読取] オフライン状態を書き込まずに表示する"
    assert r.parser_description("init") == "初期設定 (対話。`--non-interactive` で従来の非対話)"
    assert r.parser_help("news") == "[下位操作ごと] 市場ニュース源を扱う"
    assert r.leaf_help("news") is None
    assert r.parser_help("nonexistent") is None


def test_leaf_help_resolves_feature_verbs_and_conditions():
    r = _renderer()
    for feature in ("bot", "web", "notify"):
        assert r.leaf_help(f"ops {feature} enable") == (
            "[変更] 有効化する (必要時だけ daemon を一時停止して再開)")
        assert r.leaf_help(f"ops {feature} disable") == "[変更] daemon を止めずに無効化する"
        assert r.leaf_help(f"ops {feature} status") == "[読取] durable stage を表示する"
    assert r.leaf_help("ops web reconfigure") == "[変更] afx に触れずに設定を変える (別名 reconf)"
    assert r.leaf_help("ops web") is None
    assert r.leaf_help("keys init") == "[変更] 操作 API の鍵を初期化する (停止中のみ)"
    assert r.leaf_help("ctl approve") == "[変更] [確認] [approver] 承認申請を承認する"


def test_every_visible_leaf_resolves_exactly_once_in_epilogs():
    r = _renderer()
    epilog = r.render_epilog("ops")
    lines = epilog.splitlines()
    assert lines[0] == "操作:"
    assert [line.split()[3] for line in lines[1:5]] == ["enable", "disable", "status", "reconfigure"]
    assert lines[-1] == ("旧形 `afx ops enable|disable|status <feature>` は 0.2.0 だけ"
                         "受け付け、0.3.0 で削除する。")
    assert "afx improve" not in epilog
    for record in _all_exposed():
        if record.planned:
            continue
        assert r.leaf_help(record.command if record.command not in cli_surface._OPS_GENERIC
                           else record.command.replace("ops ", "ops web ")) is not None
    assert r.render_epilog("init") == ""
    assert r.render_epilog("nonexistent") == ""


def test_news_records_are_implemented_and_exposed():
    assert all(_record(f"news {action}").implemented and _record(f"news {action}").exposed
               and not _record(f"news {action}").planned
               for action in ("list", "test", "add", "enable", "disable"))
    assert not [record.command for record in cli_surface.COMMANDS if record.command == "news"]
    assert any(parent.command == "news" for parent in cli_surface.PARENTS)


def test_planned_records_never_render_even_when_marked_exposed():
    """The reservation mechanism stays guarded: a planned record must not become visible."""
    reserved = dataclasses.replace(_record("history coverage"), planned=True, implemented=False, exposed=True)
    records = tuple(reserved if r.command == "history coverage" else r for r in _all_exposed())
    with pytest.raises(ValueError):
        cli_surface.SurfaceRenderer(records)


def test_ctl_help_lists_each_group_and_each_ctl_record_once():
    text = _renderer().render_ctl_help()
    assert text.startswith("usage: afx ctl [--as operator|approver] <操作>")
    for group in G:
        assert text.count(f"{group.value}:") == 1
    for usage in ("log [n] / activity [n] [カテゴリ]", "backlog reject|reopen|note <id>",
                  "approval list [n] / approval show <id>"):
        assert usage in text
    assert "afx stop" not in text


@pytest.mark.parametrize("value", ["0.2.0rc1", "0.2.0.dev3", "0.2.0.post1", "0.2.0a2", "0.2.0b1.dev4"])
def test_version_tuple_ignores_pre_dev_and_post_suffixes(value):
    assert cli_surface.version_tuple(value) == (0, 2, 0)
    cli_surface.validate_compatibility_versions(value)


@pytest.mark.parametrize("value", ["", "x", "0..2", "0.2.", "rc1", "0.2.0-", "０.2"])
def test_version_tuple_rejects_malformed_values(value):
    with pytest.raises(ValueError):
        cli_surface.version_tuple(value)


def test_release_candidate_of_a_removal_release_is_expired():
    with pytest.raises(ValueError, match="expired compatibility record"):
        cli_surface.validate_compatibility_versions("0.3.0rc1")


def test_ctl_help_keeps_the_placeholder_for_groups_without_ctl_records():
    lines = _renderer().render_ctl_help().splitlines()
    for group in (G.MARKET, G.RESEARCH):
        assert lines[lines.index(f"{group.value}:") + 1] == "  —"
    for group in (G.OBSERVE, G.GUIDANCE, G.PLUGIN, G.RECOVERY):
        assert lines[lines.index(f"{group.value}:") + 1] != "  —"


def test_renderer_validates_once_and_lookups_do_not_rescan(monkeypatch):
    calls = []
    real = cli_surface._validate_records
    monkeypatch.setattr(cli_surface, "_validate_records",
                        lambda records: (calls.append(1), real(records))[1])
    renderer = cli_surface.SurfaceRenderer(_all_exposed())
    assert len(calls) == 1
    for command in ("ops", "ctl", "ctl killswitch", "ctl policy add"):
        renderer.parser_help(command)
        renderer.parser_description(command)
        renderer.render_epilog(command)
    renderer.render_ctl_help()
    renderer.render_shell_help()
    assert len(calls) == 1
