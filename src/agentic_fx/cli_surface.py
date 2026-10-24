"""Pure command classification data used by future CLI help renderers."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from enum import StrEnum


class Group(StrEnum):
    SETUP = "導入・接続管理"
    OBSERVE = "稼働観測・実行追跡"
    MARKET = "市場入力を整える"
    RESEARCH = "調査・評価する"
    GUIDANCE = "問い合わせ・改善運用"
    PLUGIN = "Plugin を採否・配備する"
    RECOVERY = "保護状態を回復する"


class Lane(StrEnum):
    LOCAL = "local"
    CTL = "ctl"


class Label(StrEnum):
    READ = "read"
    CHANGE = "change"
    PER_OPERATION = "per-operation"


@dataclass(frozen=True, slots=True)
class CommandRecord:
    group: Group | None
    group_order: int | None
    lane: Lane
    command: str
    usage: str
    shell_usage: str | None
    label: Label
    confirm: bool
    approver: bool
    scope: str | None
    endpoint_code: str | None
    mutating: bool
    daemon_required: bool
    condition: str | None
    summary: str
    definition: str
    aliases: tuple[str, ...]
    shell: bool
    classified: bool
    implemented: bool
    exposed: bool
    planned: bool
    deprecated_since: str | None = None
    remove_in: str | None = None


@dataclass(frozen=True, slots=True)
class AliasRecord:
    usage: str
    canonical_usage: str
    parser_alias: str


@dataclass(frozen=True, slots=True)
class CompatibilityRecord:
    usage: str
    canonical_usage: str | None
    deprecated_since: str
    remove_in: str
    message: str
    rejects: bool = False


_VERSION = re.compile(
    r"(?P<release>\d+(?:\.\d+)*)(?:\.?(?:a|b|c|rc|alpha|beta|pre|preview|dev|post)\d*)*",
    re.ASCII)


def version_tuple(value: str) -> tuple[int, ...]:
    """Parse the numeric release of a version without importing a packaging library.

    Pre-release, dev and post-release suffixes are accepted and ignored, so a
    release candidate compares as its final release.
    """
    match = _VERSION.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid version: {value!r}")
    return tuple(int(piece) for piece in match["release"].split("."))


def _record(group: Group | None, lane: Lane, command: str, usage: str, label: Label,
            summary: str, definition: str, *, shell_usage: str | None = None,
            confirm: bool = False, approver: bool = False, scope: str | None = None,
            endpoint_code: str | None = None, daemon_required: bool = False,
            condition: str | None = None, aliases: tuple[str, ...] = (), shell: bool = False,
            classified: bool = True, implemented: bool = True, exposed: bool = False,
            planned: bool = False, deprecated_since: str | None = None,
            remove_in: str | None = None) -> CommandRecord:
    return CommandRecord(
        group=group,
        group_order=(tuple(Group).index(group) + 1) if group is not None else None,
        lane=lane,
        command=command,
        usage=usage,
        shell_usage=shell_usage,
        label=label,
        confirm=confirm,
        approver=approver,
        scope=scope,
        endpoint_code=endpoint_code,
        mutating=label is not Label.READ,
        daemon_required=daemon_required,
        condition=condition,
        summary=summary,
        definition=definition,
        aliases=aliases,
        shell=shell,
        classified=classified,
        implemented=implemented,
        exposed=exposed,
        planned=planned,
        deprecated_since=deprecated_since,
        remove_in=remove_in,
    )


@dataclass(frozen=True, slots=True)
class ShellLifecycle:
    """Release window of the interactive shell, which owns its own deprecation."""

    deprecated_since: str
    remove_in: str


SHELL_LIFECYCLE = ShellLifecycle(deprecated_since="0.2.0", remove_in="1.0.0")


_BASE_COMMANDS: tuple[CommandRecord, ...] = (
    _record(Group.SETUP, Lane.LOCAL, "init", "afx init [--non-interactive] [--config FILE]",
            Label.CHANGE, "初期設定を行う", "entry.py", shell_usage=None),
    _record(Group.SETUP, Lane.LOCAL, "check", "afx check [--json] [--unit-env]", Label.READ,
            "設定と実行環境を検査する", "entry.py"),
    _record(Group.SETUP, Lane.LOCAL, "check backend",
            "afx check backend --backend <local|claude|codex|opencode> [--provider chatgpt]",
            Label.CHANGE, "backend への接続を検査する", "backtest/cli.py", implemented=False),
    _record(Group.SETUP, Lane.LOCAL, "keys init", "afx keys init", Label.CHANGE,
            "操作 API の鍵を初期化する", "entry.py", condition="停止中のみ"),
    _record(Group.SETUP, Lane.LOCAL, "keys enroll", "afx keys enroll <principal>", Label.CHANGE,
            "principal の鍵を登録する", "entry.py", condition="停止中のみ"),
    _record(Group.SETUP, Lane.LOCAL, "keys rotate", "afx keys rotate <principal>", Label.CHANGE,
            "principal の鍵を更新する", "entry.py", condition="停止中のみ"),
    _record(Group.SETUP, Lane.LOCAL, "keys revoke", "afx keys revoke <principal>", Label.CHANGE,
            "principal の鍵を失効させる", "entry.py", condition="停止中のみ"),
    _record(Group.SETUP, Lane.LOCAL, "ops enable", "afx ops <bot|web|notify> enable", Label.CHANGE,
            "機能を有効化する", "entry.py", condition="必要時だけ daemon を一時停止し、元が稼働中なら再開",
            implemented=False),
    _record(Group.SETUP, Lane.LOCAL, "ops disable", "afx ops <bot|web|notify> disable", Label.CHANGE,
            "機能を無効化する", "entry.py", implemented=False),
    _record(Group.SETUP, Lane.LOCAL, "ops web reconfigure", "afx ops web reconfigure", Label.CHANGE,
            "afx に触れずに設定を変える", "ops_setup", implemented=False),
    _record(Group.SETUP, Lane.LOCAL, "ops bot reconfigure", "afx ops bot reconfigure", Label.CHANGE,
            "afx に触れずに設定を変える", "ops_setup", implemented=False),
    _record(Group.SETUP, Lane.LOCAL, "ops notify reconfigure", "afx ops notify reconfigure", Label.CHANGE,
            "afx に触れずに設定を変える", "ops_setup", implemented=False),
    _record(Group.SETUP, Lane.LOCAL, "ops status", "afx ops <bot|web|notify> status", Label.READ,
            "機能の状態を表示する", "entry.py", implemented=False),
    _record(Group.SETUP, Lane.CTL, "ctl whoami", "afx ctl whoami", Label.READ,
            "認証した principal を表示する", "ops/client.py", shell_usage=None,
            scope="status.read", endpoint_code="whoami", daemon_required=True),

    _record(Group.OBSERVE, Lane.LOCAL, "status", "afx status", Label.READ,
            "停止中の状態を表示する", "status.py", implemented=False),
    _record(Group.OBSERVE, Lane.CTL, "ctl status", "afx ctl status", Label.READ,
            "daemon の状態を表示する", "ops/client.py", shell_usage="status", shell=True,
            scope="status.read", endpoint_code="status", daemon_required=True),
    _record(Group.OBSERVE, Lane.CTL, "ctl log", "afx ctl log [n]", Label.READ,
            "技術ログを表示する", "ops/client.py", shell_usage="log [n]", shell=True,
            scope="logs.read", endpoint_code="log", daemon_required=True),
    _record(Group.OBSERVE, Lane.CTL, "ctl activity", "afx ctl activity [n] [category]", Label.READ,
            "activity を表示する", "ops/client.py", shell_usage="activity [n] [category]", shell=True,
            scope="logs.read", endpoint_code="activity", daemon_required=True),
    _record(Group.OBSERVE, Lane.CTL, "ctl events",
            "afx ctl events [--after ID] [--limit N] [--follow]", Label.READ,
            "event を追跡する", "ops/client.py", scope="events.read", endpoint_code="events",
            daemon_required=True),
    _record(Group.OBSERVE, Lane.CTL, "ctl jobs get", "afx ctl jobs get <id>", Label.READ,
            "job の状態を表示する", "ops/client.py", scope="jobs.own", endpoint_code="job_get",
            daemon_required=True),
    _record(Group.OBSERVE, Lane.CTL, "ctl jobs cancel", "afx ctl jobs cancel <id>", Label.CHANGE,
            "待機中の job を取り消す", "ops/client.py", confirm=True, scope="jobs.own",
            endpoint_code="job_cancel", daemon_required=True),

    _record(Group.MARKET, Lane.LOCAL, "news", "afx news …", Label.PER_OPERATION,
            "市場ニュースを扱う", "news-source-cli-and-429-backoff", implemented=False, planned=True),
    _record(Group.MARKET, Lane.LOCAL, "history import", "afx history import …", Label.CHANGE,
            "履歴データを取り込む", "backtest/cli.py"),
    _record(Group.MARKET, Lane.LOCAL, "history compare", "afx history compare …", Label.READ,
            "履歴データを比較する", "backtest/cli.py"),
    _record(Group.MARKET, Lane.LOCAL, "history coverage", "afx history coverage …", Label.READ,
            "履歴データの範囲を表示する", "backtest/cli.py"),

    _record(Group.RESEARCH, Lane.LOCAL, "backtest run", "afx backtest run …", Label.CHANGE,
            "バックテストを実行する", "backtest/cli.py"),
    _record(Group.RESEARCH, Lane.LOCAL, "analyze corr", "afx analyze corr …", Label.CHANGE,
            "履歴の相関を分析する", "backtest/cli.py"),

    _record(Group.GUIDANCE, Lane.CTL, "ctl ask", "afx ctl ask <question>", Label.CHANGE,
            "質問への Mission を実行する", "ops/client.py", shell_usage="ask <質問>", shell=True,
            scope="operate", endpoint_code="ask", daemon_required=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl improve", "afx ctl improve", Label.CHANGE,
            "improve one-shot を実行する", "ops/client.py", shell_usage="improve", shell=True,
            scope="operate", endpoint_code="improve", daemon_required=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl backlog add", "afx ctl backlog add <idea>", Label.CHANGE,
            "バックログへ課題を追加する", "ops/client.py", shell_usage="backlog add <idea>", shell=True,
            scope="operate", endpoint_code="backlog_add", daemon_required=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl backlog list",
            "afx ctl backlog list [--all] [--limit N]", Label.READ,
            "バックログを表示する", "ops/client.py", scope="guidance.read", endpoint_code="backlog_list",
            daemon_required=True, implemented=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl backlog reject", "afx ctl backlog reject <id>", Label.CHANGE,
            "バックログを却下する", "ops/client.py", shell_usage="backlog reject <id>", shell=True,
            scope="operate", endpoint_code="backlog_reject", daemon_required=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl backlog reopen", "afx ctl backlog reopen <id>", Label.CHANGE,
            "バックログを再開する", "ops/client.py", shell_usage="backlog reopen <id>", shell=True,
            scope="operate", endpoint_code="backlog_reopen", daemon_required=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl backlog note", "afx ctl backlog note <id>", Label.CHANGE,
            "バックログへ注記する", "ops/client.py", shell_usage="backlog note <id>", shell=True,
            scope="operate", endpoint_code="backlog_note", daemon_required=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl policy add", "afx ctl policy add <text>", Label.CHANGE,
            "方針文を追加する", "ops/client.py", shell_usage="policy add <text>", shell=True,
            scope="operate", endpoint_code="policy", daemon_required=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl policy show", "afx ctl policy show", Label.READ,
            "管理対象の方針文を表示する", "ops/client.py", scope="guidance.read",
            endpoint_code="policy_show", daemon_required=True, implemented=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl policy remove", "afx ctl policy remove <id>", Label.CHANGE,
            "管理対象の方針文を削除する", "ops/client.py", scope="operate",
            endpoint_code="policy_remove", daemon_required=True, implemented=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl policy reconcile", "afx ctl policy reconcile", Label.CHANGE,
            "方針文の投影を再生成する", "ops/client.py", scope="operate",
            endpoint_code="policy_reconcile", daemon_required=True, implemented=True),
    _record(Group.GUIDANCE, Lane.CTL, "ctl reflect retry", "afx ctl reflect retry <order_id>", Label.CHANGE,
            "reflection を再試行対象へ戻す", "ops/client.py", shell_usage="reflect retry <order_id>",
            shell=True, scope="operate", endpoint_code="reflection_retry", daemon_required=True),

    _record(Group.PLUGIN, Lane.LOCAL, "plugin submit", "afx plugin submit <name> --from _human",
            Label.CHANGE, "plugin を提出する", "backtest/cli.py"),
    _record(Group.PLUGIN, Lane.LOCAL, "plugin bless", "afx plugin bless <name> --from _human",
            Label.CHANGE, "plugin をローカルで承認する", "backtest/cli.py"),
    _record(Group.PLUGIN, Lane.LOCAL, "plugin materialize", "afx plugin materialize <name>",
            Label.CHANGE, "plugin を実体化する", "backtest/cli.py"),
    _record(Group.PLUGIN, Lane.LOCAL, "plugin lock", "afx plugin lock <name> --from _human",
            Label.CHANGE, "plugin を固定する", "backtest/cli.py"),
    _record(Group.PLUGIN, Lane.LOCAL, "plugin retire", "afx plugin retire <name> …", Label.CHANGE,
            "plugin を引退させる", "backtest/cli.py", confirm=True, condition="運用時間外"),
    _record(Group.PLUGIN, Lane.LOCAL, "plugin disable", "afx plugin disable <name> …", Label.CHANGE,
            "plugin を無効化する", "backtest/cli.py", confirm=True, condition="再起動後反映"),
    _record(Group.PLUGIN, Lane.LOCAL, "plugin rollback", "afx plugin rollback <name> …", Label.CHANGE,
            "plugin を戻す", "backtest/cli.py", confirm=True, condition="再起動後反映"),
    _record(Group.PLUGIN, Lane.LOCAL, "plugin revoke", "afx plugin revoke <name> --version HASH …",
            Label.CHANGE, "plugin の版を失効させる", "backtest/cli.py", confirm=True),
    _record(Group.PLUGIN, Lane.LOCAL, "plugin versions", "afx plugin versions <name>", Label.READ,
            "plugin の版を表示する", "backtest/cli.py"),
    _record(Group.PLUGIN, Lane.LOCAL, "plugin list", "afx plugin list [--kind indicator|strategy]",
            Label.READ, "plugin を一覧表示する", "backtest/cli.py", implemented=False),
    _record(Group.PLUGIN, Lane.CTL, "ctl approval list", "afx ctl approval list [n]", Label.READ,
            "承認待ちを表示する", "ops/client.py", shell_usage="approval list [n]", shell=True,
            scope="approvals.list", endpoint_code="approval_list", daemon_required=True),
    _record(Group.PLUGIN, Lane.CTL, "ctl approval show", "afx ctl approval show <id>", Label.READ,
            "承認申請の詳細を表示する", "ops/client.py", shell_usage="approval show <id>", shell=True,
            scope="approvals.detail", endpoint_code="approval_detail", daemon_required=True),
    _record(Group.PLUGIN, Lane.CTL, "ctl approve", "afx ctl approve <id>", Label.CHANGE,
            "承認申請を承認する", "ops/client.py", shell_usage="approve <id>", shell=True,
            confirm=True, approver=True, scope="decide", endpoint_code="approve", daemon_required=True),
    _record(Group.PLUGIN, Lane.CTL, "ctl reject", "afx ctl reject <id> [reason]", Label.CHANGE,
            "承認申請を却下する", "ops/client.py", shell_usage="reject <id> [理由]", shell=True,
            confirm=True, approver=True, scope="decide", endpoint_code="reject", daemon_required=True),
    _record(Group.PLUGIN, Lane.CTL, "ctl approval retry", "afx ctl approval retry <id>", Label.CHANGE,
            "承認手順を再試行する", "ops/client.py", shell_usage="approval retry <id>", shell=True,
            confirm=True, approver=True, scope="decide", endpoint_code="retry", daemon_required=True),

    _record(Group.RECOVERY, Lane.CTL, "ctl killswitch reset", "afx ctl killswitch reset", Label.READ,
            "kill switch の世代を表示する", "ops/client.py", shell_usage="killswitch reset", shell=True,
            scope="status.read", endpoint_code="status", daemon_required=True),
    _record(Group.RECOVERY, Lane.CTL, "ctl killswitch reset",
            "afx ctl killswitch reset <generation>", Label.CHANGE, "kill switch を解除する",
            "ops/client.py", shell_usage="killswitch reset <世代>", shell=True, confirm=True,
            approver=True, scope="local_guard", endpoint_code="killswitch_reset", daemon_required=True),
    _record(Group.RECOVERY, Lane.CTL, "ctl killswitch reconcile",
            "afx ctl killswitch reconcile [confirm]", Label.CHANGE, "kill switch の状態を確定する",
            "ops/client.py", shell_usage="killswitch reconcile [confirm]", shell=True, confirm=True,
            approver=True, scope="local_guard", endpoint_code="killswitch_reconcile", daemon_required=True),
    _record(Group.RECOVERY, Lane.CTL, "ctl data resume", "afx ctl data resume [--acknowledge]",
            Label.CHANGE, "価格源の手動復帰を要求する", "ops/client.py",
            shell_usage="data resume [--acknowledge]", shell=True, confirm=True, approver=True,
            scope="local_guard", endpoint_code="data_resume", daemon_required=True),

    _record(None, Lane.CTL, "stop", "afx stop", Label.CHANGE, "daemon を graceful shutdown する",
            "commands.py", shell_usage="stop", shell=True, classified=False,
            deprecated_since=SHELL_LIFECYCLE.deprecated_since,
            remove_in=SHELL_LIFECYCLE.remove_in),
)


ALIASES: tuple[AliasRecord, ...] = (
    AliasRecord("afx ctl improve add <idea>", "afx ctl backlog add <idea>", "improve add"),
    AliasRecord("afx ops web reconf", "afx ops web reconfigure", "reconf"),
    AliasRecord("afx ops bot reconf", "afx ops bot reconfigure", "reconf"),
    AliasRecord("afx ops notify reconf", "afx ops notify reconfigure", "reconf"),
    AliasRecord("afx ctl approvals list [n]", "afx ctl approval list [n]", "approvals list"),
    AliasRecord("afx ctl approvals show <id>", "afx ctl approval show <id>", "approvals show"),
    AliasRecord("afx ctl approval <id>", "afx ctl approval show <id>", "approval <id>"),
    AliasRecord("afx ctl retry <id>", "afx ctl approval retry <id>", "retry <id>"),
)


def _with_aliases(records: tuple[CommandRecord, ...],
                  aliases: tuple[AliasRecord, ...]) -> tuple[CommandRecord, ...]:
    """Fill each record's parser aliases from the single alias table."""
    return tuple(replace(record, aliases=tuple(
        alias.parser_alias for alias in aliases if alias.canonical_usage == record.usage))
        for record in records)


COMMANDS: tuple[CommandRecord, ...] = _with_aliases(_BASE_COMMANDS, ALIASES)


COMPATIBILITY_RECORDS: tuple[CompatibilityRecord, ...] = (
    CompatibilityRecord(
        "afx improve …",
        "afx check backend --backend <name> [--provider chatgpt]",
        "0.2.0",
        "0.3.0",
        "afx improve verify-backend は廃止されました。afx check backend --backend <name> "
        "[--provider chatgpt] を使ってください。",
        rejects=True,
    ),
    CompatibilityRecord(
        "afx ops <enable|disable|status> <bot|web|notify> …",
        "afx ops <bot|web|notify> <enable|disable|status> …",
        "0.2.0",
        "0.3.0",
        "afx ops <verb> <feature> は旧形です。afx ops <feature> <verb> を使ってください。"
        "この形は 0.3.0 で削除します。",
    ),
)


def validate_compatibility_versions(
        package_version: str,
        compatibility: tuple[CompatibilityRecord, ...] | None = None,
        commands: tuple[CommandRecord, ...] | None = None,
        shell_lifecycle: ShellLifecycle | None = None) -> None:
    """Reject metadata that survives past its promised removal release."""
    current = version_tuple(package_version)
    compatibility = COMPATIBILITY_RECORDS if compatibility is None else compatibility
    commands = COMMANDS if commands is None else commands
    shell_lifecycle = SHELL_LIFECYCLE if shell_lifecycle is None else shell_lifecycle
    if current >= version_tuple(shell_lifecycle.remove_in):
        raise ValueError("expired compatibility record: shell")
    for record in compatibility:
        if current >= version_tuple(record.remove_in):
            raise ValueError(f"expired compatibility record: {record.usage}")
    for command in commands:
        if command.remove_in is not None and current >= version_tuple(command.remove_in):
            raise ValueError(f"expired compatibility record: {command.usage}")


def _validate_records(records: tuple[CommandRecord, ...]) -> None:
    usages = [record.usage for record in records]
    if len(set(usages)) != len(usages):
        raise ValueError("duplicate record usage")
    for record in records:
        if record.exposed and not record.implemented:
            raise ValueError(f"exposed record is not implemented: {record.usage}")
        if record.planned and record.exposed:
            raise ValueError(f"planned record is exposed: {record.usage}")
        if record.classified != (record.group is not None):
            raise ValueError(f"classification does not match group: {record.usage}")
        if record.group is not None and record.group_order != tuple(Group).index(record.group) + 1:
            raise ValueError(f"wrong group order: {record.usage}")
        if (record.deprecated_since is None) != (record.remove_in is None):
            raise ValueError(f"incomplete lifecycle fields: {record.usage}")
        if (record.remove_in is not None
                and version_tuple(record.deprecated_since) >= version_tuple(record.remove_in)):
            raise ValueError(f"lifecycle is not ordered: {record.usage}")



@dataclass(frozen=True, slots=True)
class ParentRecord:
    """A command group that has no operation of its own, only sub-operations."""

    command: str
    summary: str
    labeled: bool = True


PARENTS: tuple[ParentRecord, ...] = (
    ParentRecord("check", "設定と実行環境の検査"),
    ParentRecord("keys", "操作 API の鍵管理"),
    ParentRecord("ops", "bot / web / notify の有効化・無効化・再設定・状態表示"),
    ParentRecord("ops bot", "操作 bot (ops-bot)", labeled=False),
    ParentRecord("ops web", "web 表示 (ops-web)", labeled=False),
    ParentRecord("ops notify", "通知 (ops-notify)", labeled=False),
    ParentRecord("ctl", "稼働中の daemon を操作 API で操作する", labeled=False),
    ParentRecord("ctl jobs", "ジョブの参照と取消"),
    ParentRecord("ctl backlog", "改善バックログの操作"),
    ParentRecord("ctl policy", "ユーザー方針 (directives) の操作"),
    ParentRecord("ctl reflect", "反省処理の操作"),
    ParentRecord("ctl approval", "承認申請の表示と再試行"),
    ParentRecord("ctl killswitch", "kill switch の表示・解除・状態確定"),
    ParentRecord("ctl data", "価格源の復旧操作"),
    ParentRecord("history", "履歴データ操作"),
    ParentRecord("backtest", "バックテスト"),
    ParentRecord("analyze", "履歴分析"),
    ParentRecord("plugin", "plugin の採否・配備・表示"),
)

_OPS_FEATURES = ("bot", "web", "notify")
_OPS_GENERIC = frozenset({"ops enable", "ops disable", "ops status"})


def _labels(record: CommandRecord) -> str:
    if record.label is Label.PER_OPERATION:
        values = ["[下位操作ごと]"]
    else:
        values = ["[読取]" if record.label is Label.READ else "[変更]"]
    if record.confirm:
        values.append("[確認]")
    if record.approver:
        values.append("[approver]")
    return " ".join(values)


def _expand(record: CommandRecord) -> tuple[str, ...]:
    """Concrete command paths a record answers to (feature-generic ops verbs fan out)."""
    if record.command in _OPS_GENERIC:
        verb = record.command.split(" ", 1)[1]
        return tuple(f"ops {feature} {verb}" for feature in _OPS_FEATURES)
    return (record.command,)


class SurfaceRenderer:
    """Read-only view of metadata; parser integration supplies this in a later task."""

    def __init__(self, records: tuple[CommandRecord, ...] = COMMANDS,
                 parents: tuple[ParentRecord, ...] = PARENTS,
                 compatibility: tuple[CompatibilityRecord, ...] = COMPATIBILITY_RECORDS,
                 shell_lifecycle: ShellLifecycle = SHELL_LIFECYCLE) -> None:
        _validate_records(records)
        self._records = records
        self._parents = {parent.command: parent for parent in parents}
        self._compatibility = compatibility
        self._shell_lifecycle = shell_lifecycle
        self._visible = tuple(record for record in records
                              if record.exposed and not record.planned)
        by_path: dict[str, list[CommandRecord]] = {}
        by_usage: dict[str, CommandRecord] = {}
        children: dict[str, list[CommandRecord]] = {}
        for record in self._visible:
            by_usage.setdefault(record.usage, record)
            for path in _expand(record):
                by_path.setdefault(path, []).append(record)
                words = path.split(" ")
                for size in range(1, len(words)):
                    siblings = children.setdefault(" ".join(words[:size]), [])
                    if not any(record is seen for seen in siblings):
                        siblings.append(record)
        self._by_path = {path: tuple(found) for path, found in by_path.items()}
        self._by_usage = by_usage
        self._children_of = {path: tuple(found) for path, found in children.items()}

    def _exacts(self, command: str) -> tuple[CommandRecord, ...]:
        """Records answering to one parser node; a read and a change form can share it."""
        if command in self._by_usage:
            return (self._by_usage[command],)
        return self._by_path.get(command, ())

    def _exact(self, command: str) -> CommandRecord | None:
        found = self._exacts(command)
        return found[0] if found else None

    def _children(self, command: str) -> tuple[CommandRecord, ...]:
        return self._children_of.get(command, ())

    def top_description(self) -> str:
        return "agentic-fx"

    def render_top_epilog(self) -> str:
        return "実行レーン:\n  afx <sub>      daemon を介さない\n  afx ctl <op>   稼働 daemon を操作する"

    def _parent_label(self, children: tuple[CommandRecord, ...]) -> str:
        kinds = {record.label for record in children}
        if len(kinds) == 1 and Label.PER_OPERATION not in kinds:
            return "[読取]" if Label.READ in kinds else "[変更]"
        return "[下位操作ごと]"

    def parser_help(self, command: str) -> str | None:
        children = self._children(command)
        exacts = self._exacts(command)
        if children:
            parent = self._parents.get(command)
            summary = parent.summary if parent else (exacts[0].summary if exacts else None)
            if summary is None:
                return None
            if parent is not None and not parent.labeled:
                return summary
            return f"{self._parent_label((*exacts, *children))} {summary}"
        if len(exacts) > 1:
            return f"{self._parent_label(exacts)} " + " / ".join(r.summary for r in exacts)
        return f"{_labels(exacts[0])} {exacts[0].summary}" if exacts else None

    def parser_description(self, command: str) -> str | None:
        children = self._children(command)
        exacts = self._exacts(command)
        if children:
            parent = self._parents.get(command)
            if parent is not None:
                return parent.summary
            return exacts[0].summary if exacts else None
        if len(exacts) > 1:
            return " / ".join(r.summary for r in exacts)
        return exacts[0].summary if exacts else None

    def leaf_help(self, command: str) -> str | None:
        record = self._exact(command)
        if record is None:
            return None
        suffix = f" ({record.condition})" if record.condition else ""
        return f"{_labels(record)} {record.summary}{suffix}"

    def render_epilog(self, command: str) -> str:
        records = self._children(command)
        if not records:
            return ""
        lines = ["操作:"]
        for record in records:
            suffix = f" ({record.condition})" if record.condition else ""
            lines.append(f"  {record.usage}  {_labels(record)} {record.summary}{suffix}")
        legacy = [item for item in self._compatibility
                  if not item.rejects and item.usage.startswith(f"afx {command} ")]
        if legacy:
            lines.append("")
            lines.extend(f"旧形 `{item.usage}` は {item.deprecated_since} だけ受け付け、"
                         f"{item.remove_in} で削除する。" for item in legacy)
        return "\n".join(lines)

    def render_ctl_help(self) -> str:
        lines = ["usage: afx ctl [--as operator|approver] <操作>", ""]
        for group in Group:
            records = tuple(record for record in self._visible
                            if record.lane is Lane.CTL and record.group is group)
            lines.append(f"{group.value}:")
            lines.extend(f"  {_labels(record)} {record.usage.removeprefix('afx ctl ')}"
                         for record in records)
            if not records:
                lines.append("  —")
            lines.append("")
        return "\n".join(lines).rstrip()

    def render_shell_help(self) -> str:
        records = tuple(record for record in self._visible
                        if record.shell and record.shell_usage is not None)
        if not records:
            return ""
        lines = ["コマンド一覧:"]
        lines.extend(f"  {record.shell_usage}  {record.summary}" for record in records)
        lifecycle = self._shell_lifecycle
        lines.append(f"廃止予定: 対話 shell は {lifecycle.deprecated_since} で deprecated。"
                     f"{lifecycle.remove_in} で削除予定。afx ctl を使用してください。")
        return "\n".join(lines)


RENDERER = SurfaceRenderer()


def top_description() -> str:
    return RENDERER.top_description()


def render_top_epilog() -> str:
    return RENDERER.render_top_epilog()


def parser_help(command: str) -> str | None:
    return RENDERER.parser_help(command)


def parser_description(command: str) -> str | None:
    return RENDERER.parser_description(command)


def leaf_help(command: str) -> str | None:
    return RENDERER.leaf_help(command)


def render_epilog(command: str) -> str:
    return RENDERER.render_epilog(command)


def render_ctl_help() -> str:
    return RENDERER.render_ctl_help()


def render_shell_help() -> str:
    return RENDERER.render_shell_help()
