"""plugin の read model (一覧と 1 件の詳細)。

入力 (FS の entry、DB の行、settings) を受け取る純関数で、FS も DB も読まない。
daemon の API と CLI の `afx plugin list` が、それぞれ入力を集めて同じ関数を呼ぶ
(入力の集め方は `read_model_inputs`)。

状態は 3 つの独立した軸で持ち、1 つにまとめない。

- 配備実体 (``entity``): ``symlink`` (版つき) / ``plain`` / ``absent`` / ``unreadable``
- 承認と読込 (``admission``): ``loaded`` / ``excluded`` / ``unapproved`` / ``unknown`` /
  ``none`` (読める実体が無い)
- 承認待ち (``pending``): 承認待ちの依頼

読込除外・読めない理由は固定語だけを出す。loader の自由文 (例外の文面など) は
入力に載せない。切替の途中の状態は ``open_ops`` として出すだけで、lock は取らない。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

from agentic_fx.plugin import usage_review
from agentic_fx.plugin.loader import PluginMeta, is_canonical_plugin_name
from agentic_fx.plugin.resolve import (
    STATE_MATCH, STATE_UNKNOWN, AliasState, ApprovedInventory, enumerate_indicator_states,
)
from agentic_fx.store import plugin_deploy_ops as deploy_store
from agentic_fx.store.signal_forward import ForwardTarget, ForwardTargets
from agentic_fx.tools.plugin_loader import approved_hashes_from_rows

KIND_STRATEGY = "strategy"
KIND_INDICATOR = "indicator"
KIND_SIGNAL = "signal"
KIND_UNKNOWN = "unknown"
PLUGIN_KINDS = (KIND_STRATEGY, KIND_INDICATOR, KIND_SIGNAL)
KINDS = (*PLUGIN_KINDS, KIND_UNKNOWN)

ENTITY_SYMLINK = "symlink"
ENTITY_PLAIN = "plain"
ENTITY_ABSENT = "absent"
ENTITY_UNREADABLE = "unreadable"

ADMISSION_LOADED = "loaded"
ADMISSION_EXCLUDED = "excluded"
ADMISSION_UNAPPROVED = "unapproved"
ADMISSION_UNKNOWN = "unknown"
ADMISSION_NONE = "none"

# 実体が読めない理由 (固定語)。
UNREADABLE_REASONS = frozenset({
    "missing_files", "invalid_manifest", "invalid_symlink", "version_mismatch",
    "io_error", "non_canonical_name",
})
# 承認が無い理由 (固定語)。
UNAPPROVED_REASONS = frozenset({"never_approved", "content_changed"})
EXCLUDED_REASON = "indicator_unresolved"

SOURCE_OK = "ok"
SOURCE_MISSING = "missing"
SOURCE_UNAVAILABLE = "unavailable"

SUBMISSION_ORIGINS = frozenset({"staging", "human"})
KNOWN_ORIGINS = frozenset({"staging", "human", "reevaluation"})
REEVALUATION_ORIGIN = "reevaluation"

HASH_PATTERN = re.compile(r"[0-9a-fA-F]{12,}")
_DISPLAY_HASH = re.compile(r"[0-9a-fA-F]{12,128}")
_ARTIFACT_HASH = re.compile(r"[0-9a-f]{64}")
HISTORY_LIMIT = 50


# ---------------------------------------------------------------- 入力

@dataclass(frozen=True, slots=True)
class LiveEntry:
    """``plugins/`` の 1 entry。``meta`` は読めたときだけ。``reason`` は固定語。"""
    entry_name: str
    state: str                       # symlink | plain | unreadable
    artifact_hash: str | None = None
    meta: PluginMeta | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LiveScan:
    status: str = SOURCE_OK
    root: Path | None = None
    entries: tuple[LiveEntry, ...] = ()
    # ``.versions/<name>/<artifact_hash>`` の一覧 (名前 -> 版の hash)。
    versions: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # ``.versions`` の一部が読めなかった (一時的な失敗かもしれない)。
    versions_unread: bool = False


@dataclass(frozen=True, slots=True)
class ApprovalRow:
    id: int
    status: str
    decided_at: str | None
    created_at: str | None
    payload_json: str


@dataclass(frozen=True, slots=True)
class DeployOpRow:
    op_id: int
    op_kind: str
    name: str
    phase: str
    fail_reason: str | None
    subject_content_hash: str | None
    target_content_hash: str | None
    target_artifact_hash: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class JournalRow:
    op_id: int
    kind: str
    approval_id: int
    name: str
    phase: str
    new_target: str
    switch_required: int = 1
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class ReadInputs:
    live: LiveScan = field(default_factory=LiveScan)
    approvals: tuple[ApprovalRow, ...] = ()
    deploy_ops: tuple[DeployOpRow, ...] = ()
    journals: tuple[JournalRow, ...] = ()
    pending_status: str = SOURCE_OK
    journal_status: str = SOURCE_OK
    candidates: tuple[usage_review.CandidateRow, ...] = ()
    candidates_status: str = SOURCE_OK


# ---------------------------------------------------------------- 出力

@dataclass(frozen=True, slots=True)
class Entity:
    state: str
    artifact_hash: str | None = None
    content_hash: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class Admission:
    state: str
    reason: str | None = None
    alias: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class PendingItem:
    id: int
    content_hash: str
    kind: str | None


@dataclass(frozen=True, slots=True)
class OpenOp:
    source: str                      # deploy | switch
    op_id: int
    kind: str
    target: str


@dataclass(frozen=True, slots=True)
class Dependent:
    strategy: str
    alias: str
    pin: str | None
    state: str


@dataclass(frozen=True, slots=True)
class HistoryItem:
    id: int
    status: str
    kind: str | None
    content_hash: str | None
    artifact_hash: str | None
    origin: str | None
    mission_id: int | None
    decided_at: str | None
    created_at: str | None


@dataclass(frozen=True, slots=True)
class VersionItem:
    artifact_hash: str
    is_live: bool
    approval_id: int | None
    status: str | None
    content_hash: str | None


@dataclass(frozen=True, slots=True)
class Usefulness:
    submitted: int = 0
    deployed: int = 0
    by_pin: Mapping[str, tuple[int, int]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PluginRow:
    name: str
    kind: str
    entity: Entity
    live_hashes: tuple[str, ...]
    admission: Admission
    pending: tuple[PendingItem, ...]
    open_ops: tuple[OpenOp, ...]
    issues: tuple[str, ...]
    deps: tuple[AliasState, ...]
    deps_set_state: str | None
    dependents: tuple[Dependent, ...]
    usefulness: Usefulness | None
    history: tuple[HistoryItem, ...]
    history_total: int
    versions: tuple[VersionItem, ...]
    # 配備中の指標だけが持つ、棚卸しの事実 (判定は読む時刻で行う)
    usage: usage_review.UsageFacts | None = None


@dataclass(frozen=True, slots=True)
class ReadModel:
    sources: Mapping[str, str]
    rows: tuple[PluginRow, ...]
    invalid_pending: tuple[int, ...]
    unknown_submissions: int
    usage: usage_review.UsageScan = field(default_factory=usage_review.UsageScan)

    def by_name(self, name: str) -> PluginRow | None:
        for row in self.rows:
            if row.name == name:
                return row
        return None


# ---------------------------------------------------------------- 入力の解釈

@dataclass(frozen=True, slots=True)
class _Request:
    id: int
    status: str
    decided_at: str | None
    created_at: str | None
    payload: dict | None
    name: str | None
    kind: str | None                 # payload の kind (4 種に入らなければ None)
    content_hash: str | None
    artifact_hash: str | None
    origin: str | None
    mission_id: int | None


def _load_payload(raw: object) -> dict | None:
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_request(row: ApprovalRow) -> _Request:
    payload = _load_payload(row.payload_json)
    if payload is None:
        return _Request(row.id, row.status, row.decided_at, row.created_at,
                        None, None, None, None, None, None, None)
    kind = payload.get("kind")
    origin = payload.get("candidate_origin")
    mission = payload.get("mission_id")
    return _Request(
        row.id, row.status, row.decided_at, row.created_at, payload,
        _text(payload.get("name")),
        kind if kind in PLUGIN_KINDS else None,
        _text(payload.get("content_hash")), _text(payload.get("artifact_hash")),
        origin if isinstance(origin, str) and origin in KNOWN_ORIGINS else None,
        mission if type(mission) is int else None)


def _live_metas(scan: LiveScan) -> list[PluginMeta]:
    return [entry.meta for entry in scan.entries if entry.meta is not None]


def _open_deploy(row: DeployOpRow) -> bool:
    return deploy_store.is_open({"phase": row.phase, "fail_reason": row.fail_reason})


def _reason_of(entry: LiveEntry) -> str:
    return entry.reason if entry.reason in UNREADABLE_REASONS else "invalid_manifest"


def _open_journal(row: JournalRow) -> bool:
    return row.phase not in ("decided", "reverted")


def _is_pending_valid(request: _Request) -> bool:
    return (request.name is not None and is_canonical_plugin_name(request.name)
            and request.content_hash is not None
            and HASH_PATTERN.fullmatch(request.content_hash) is not None)


# ---------------------------------------------------------------- 有用性

def _dependency_pins(payload: dict) -> dict[str, set[str]] | None:
    """依頼の ``indicator_deps`` を indicator 名 -> pin の集合に直す。
    key が無い (古い依頼) ときは None。alias が違っても同じ indicator は 1 つにまとまる。"""
    if "indicator_deps" not in payload:
        return None
    refs = payload["indicator_deps"]
    if not isinstance(refs, dict):
        return None
    pins: dict[str, set[str]] = {}
    for ref in refs.values():
        if not isinstance(ref, dict) or not isinstance(ref.get("plugin"), str):
            continue
        pin = ref.get("content_hash")
        pins.setdefault(ref["plugin"], set()).add(pin if isinstance(pin, str) else "")
    return pins


def _count_usefulness(requests: Sequence[_Request], decided_approval_ids: set[int]
                     ) -> tuple[dict[str, Usefulness], int]:
    """indicator ごとの提出件数と配備成功件数、依存の記録が無い依頼の件数。

    数える対象は kind=strategy の新規提出 (改善の提出と CLI の submit / bless) の
    依頼で、再評価の依頼は除く。依頼単位で数える。配備成功は、その依頼の決定で
    switch journal が ``decided`` まで進んだもの。"""
    submitted: dict[str, int] = {}
    deployed: dict[str, int] = {}
    by_pin: dict[str, dict[str, list[int]]] = {}
    unknown = 0
    for request in requests:
        if request.payload is None or request.kind != KIND_STRATEGY:
            continue
        if request.payload.get("candidate_origin") == REEVALUATION_ORIGIN:
            continue
        pins = _dependency_pins(request.payload)
        if pins is None:
            unknown += 1
            continue
        success = request.id in decided_approval_ids
        for plugin, plugin_pins in pins.items():
            submitted[plugin] = submitted.get(plugin, 0) + 1
            deployed[plugin] = deployed.get(plugin, 0) + (1 if success else 0)
            for pin in plugin_pins:
                counts = by_pin.setdefault(plugin, {}).setdefault(pin, [0, 0])
                counts[0] += 1
                counts[1] += 1 if success else 0
    result = {
        plugin: Usefulness(
            submitted[plugin], deployed[plugin],
            {pin: (counts[0], counts[1]) for pin, counts in sorted(by_pin[plugin].items())})
        for plugin in submitted}
    return result, unknown


# ---------------------------------------------------------------- 組み立て

def build_read_model(inputs: ReadInputs, *, settings) -> ReadModel:
    """入力から read model を作る。``settings`` が None のときは依存の解決を試みず、
    依存を持つ strategy の状態は「不明」にする。"""
    scan = inputs.live
    requests = [_parse_request(row) for row in inputs.approvals]
    by_name_requests: dict[str, list[_Request]] = {}
    for request in requests:
        if request.name is not None:
            by_name_requests.setdefault(request.name, []).append(request)

    entries_by_name: dict[str, list[LiveEntry]] = {}
    for entry in scan.entries:
        key = entry.meta.name if entry.meta is not None else entry.entry_name
        entries_by_name.setdefault(key, []).append(entry)

    parseable = [
        {"id": row.id, "status": row.status, "decided_at": row.decided_at,
         "payload_json": row.payload_json}
        for row, request in zip(inputs.approvals, requests) if request.payload is not None]
    approved = approved_hashes_from_rows(parseable)

    metas = _live_metas(scan)
    phase1 = [meta for meta in metas if meta.content_hash in approved.get(meta.name, ())]
    root = scan.root if scan.root is not None else Path(".")
    indicator_inventory = ApprovedInventory(
        root=root, metas=tuple(meta for meta in phase1 if meta.kind == KIND_INDICATOR))
    phase1_keys = {(meta.name, meta.content_hash) for meta in phase1}

    # strategy ごとの依存の状態 (読める strategy の全部。承認の有無は admission に出す)。
    dependency: dict[str, tuple[tuple[AliasState, ...], str | None]] = {}
    for meta in metas:
        if meta.kind != KIND_STRATEGY or not meta.indicators:
            continue
        if settings is None:
            dependency[meta.name] = (tuple(
                AliasState(ref.alias, ref.plugin, ref.pin, STATE_UNKNOWN)
                for ref in meta.indicators), None)
            continue
        states = enumerate_indicator_states(meta, indicator_inventory, settings=settings)
        dependency[meta.name] = (states.aliases, states.set_state)

    dependents: dict[str, list[Dependent]] = {}
    for strategy_name, (aliases, _set_state) in sorted(dependency.items()):
        for item in sorted(aliases, key=lambda alias: alias.alias):
            dependents.setdefault(item.plugin, []).append(
                Dependent(strategy_name, item.alias, item.pin, item.state))

    decided_ids = {journal.approval_id for journal in inputs.journals
                   if journal.phase == "decided" and journal.kind in ("approve", "bless")}
    usefulness, unknown_submissions = _count_usefulness(requests, decided_ids)

    names: set[str] = set(entries_by_name)
    names.update(scan.versions)
    names.update(name for name in by_name_requests)
    names.update(op.name for op in inputs.deploy_ops)
    names.update(journal.name for journal in inputs.journals)
    names.discard("")

    open_deploy: dict[str, list[OpenOp]] = {}
    for op in inputs.deploy_ops:
        if _open_deploy(op):
            target = op.target_content_hash or op.subject_content_hash or "-"
            open_deploy.setdefault(op.name, []).append(OpenOp(
                "deploy", op.op_id, op.op_kind.removesuffix("_symlink"), target))
    for journal in inputs.journals:
        if _open_journal(journal):
            open_deploy.setdefault(journal.name, []).append(OpenOp(
                "switch", journal.op_id, journal.kind,
                str(journal.new_target).rsplit("/", 1)[-1]))

    rows = [
        _build_row(
            name, entries_by_name.get(name, []), by_name_requests.get(name, []),
            scan, approved, phase1_keys, dependency, dependents, usefulness,
            open_deploy.get(name, []), settings)
        for name in sorted(names)]
    usage_scan, usage_facts = _usage_review(
        inputs, rows, requests, dependents,
        scan_failed=_scan_failed(scan), live_unreadable=_live_unreadable(scan))
    rows = [replace(row, usage=usage_facts[row.name]) if row.name in usage_facts else row
            for row in rows]

    invalid_pending = tuple(
        request.id for request in requests
        if request.status == "pending" and not _is_pending_valid(request))
    return ReadModel(
        sources={"live": scan.status, "pending": inputs.pending_status,
                 "journal": inputs.journal_status},
        rows=tuple(rows), invalid_pending=invalid_pending,
        unknown_submissions=unknown_submissions, usage=usage_scan)


def _build_row(name, entries, requests, scan, approved, phase1_keys, dependency,
               dependents, usefulness, open_ops, settings) -> PluginRow:
    readable = [entry for entry in entries if entry.meta is not None]
    issues: set[str] = set()
    if len(readable) > 1:
        issues.add("duplicate_live")
    for entry in entries:
        if entry.meta is None:
            issues.add(f"broken:{_reason_of(entry)}")
    meta = readable[0].meta if readable else None

    if meta is not None:
        kind = meta.kind if meta.kind in PLUGIN_KINDS else KIND_UNKNOWN
    else:
        newest = max(requests, key=lambda request: request.id, default=None)
        kind = newest.kind if newest is not None and newest.kind is not None else KIND_UNKNOWN

    if readable:
        first = readable[0]
        entity = Entity(first.state, first.artifact_hash, meta.content_hash)
    elif entries:
        entity = Entity(ENTITY_UNREADABLE, entries[0].artifact_hash, None, entries[0].reason)
    else:
        entity = Entity(ENTITY_ABSENT)
    live_hashes = tuple(entry.meta.content_hash for entry in readable)

    pending_requests = [request for request in requests
                        if request.status == "pending" and _is_pending_valid(request)]
    pending: list[PendingItem] = []
    for request in pending_requests:
        pending.append(PendingItem(request.id, request.content_hash, request.kind))
        if request.kind is None:
            issues.add("kind_missing")
        elif meta is not None and request.kind != meta.kind:
            issues.add("kind_mismatch")
    if len({item.content_hash for item in pending}) != len(pending):
        issues.add("duplicate_pending")
    if len(open_ops) > 1:
        issues.add("duplicate_open_op")

    if meta is None:
        admission = Admission(ADMISSION_NONE)
    elif (meta.name, meta.content_hash) not in phase1_keys:
        reason = "content_changed" if approved.get(meta.name) else "never_approved"
        admission = Admission(ADMISSION_UNAPPROVED, reason)
    elif meta.kind == KIND_STRATEGY and meta.indicators:
        aliases, set_state = dependency.get(meta.name, ((), None))
        failing = next((item for item in aliases if item.state != STATE_MATCH), None)
        if settings is None:
            admission = Admission(ADMISSION_UNKNOWN)
        elif failing is not None:
            admission = Admission(ADMISSION_EXCLUDED, EXCLUDED_REASON, failing.alias,
                                  failing.state)
        elif set_state is not None:
            admission = Admission(ADMISSION_EXCLUDED, EXCLUDED_REASON, None, set_state)
        else:
            admission = Admission(ADMISSION_LOADED)
    else:
        admission = Admission(ADMISSION_LOADED)

    aliases, set_state = dependency.get(meta.name, ((), None)) if meta is not None else ((), None)

    useful = usefulness.get(name)
    if useful is None and kind == KIND_INDICATOR:
        useful = Usefulness()

    history_all = tuple(sorted(
        (HistoryItem(request.id, request.status, request.kind, request.content_hash,
                     request.artifact_hash, request.origin, request.mission_id,
                     request.decided_at, request.created_at)
         for request in requests if request.payload is not None),
        key=lambda item: -item.id))
    history = history_all[:HISTORY_LIMIT]

    live_artifact = entity.artifact_hash if entity.state == ENTITY_SYMLINK else None
    latest_by_artifact: dict[str, _Request] = {}
    for request in sorted(requests, key=lambda item: item.id):
        if request.artifact_hash is not None:
            latest_by_artifact[request.artifact_hash] = request
    versions = tuple(
        VersionItem(
            artifact, artifact == live_artifact,
            latest_by_artifact[artifact].id if artifact in latest_by_artifact else None,
            latest_by_artifact[artifact].status if artifact in latest_by_artifact else None,
            latest_by_artifact[artifact].content_hash if artifact in latest_by_artifact
            else None)
        for artifact in sorted(scan.versions.get(name, ())))

    return PluginRow(
        name=name, kind=kind, entity=entity, live_hashes=live_hashes, admission=admission,
        pending=tuple(pending), open_ops=tuple(open_ops), issues=tuple(sorted(issues)),
        deps=aliases, deps_set_state=set_state,
        dependents=tuple(dependents.get(name, ())), usefulness=useful,
        history=history, history_total=len(history_all), versions=versions)


# ---------------------------------------------------------------- 指標の棚卸し

def _scan_failed(scan: LiveScan) -> bool:
    """配備の走査を読み切れなかったか (一時的な失敗かもしれない entry を含む)。"""
    return scan.status == SOURCE_UNAVAILABLE or any(
        entry.reason == "io_error" for entry in scan.entries)


def _live_unreadable(scan: LiveScan) -> bool:
    """読めない配備中の entry があるか。meta が無いと kind を知れず、strategy かもしれない。"""
    return any(entry.meta is None and entry.reason != "non_canonical_name"
               for entry in scan.entries)


def _pending_condition(inputs: ReadInputs, requests: Sequence[_Request]
                       ) -> tuple[dict[str, bool], str | None]:
    """承認待ちの依頼が参照する指標名と、読み切れない依頼があるときの理由。

    戦略の依頼で依存の記録が読めないもの、記録の一部が壊れているものは、どの指標を参照
    するか分からない。"""
    referenced: dict[str, bool] = {}
    if inputs.pending_status == SOURCE_UNAVAILABLE:
        return referenced, usage_review.REASON_PENDING_SOURCE
    unreadable = False
    for request in requests:
        if request.status != "pending":
            continue
        if request.payload is None:
            unreadable = True
            continue
        refs = request.payload.get("indicator_deps")
        if isinstance(refs, dict):
            for ref in refs.values():
                if isinstance(ref, dict) and isinstance(ref.get("plugin"), str):
                    referenced[ref["plugin"]] = True
                else:
                    unreadable = True      # 読める参照は使用中とし、残りは決められない
        elif request.kind in (KIND_STRATEGY, None):
            unreadable = True
    return referenced, (usage_review.REASON_PENDING_REQUEST if unreadable else None)


def _current_origin(requests: Sequence[_Request], content_hash: str) -> str:
    """いまの版の出所。同じ内容の承認済みの提出 (改善か人) のうち最新のもの。

    再評価の依頼は元の版の出所を継ぐので、提出の依頼が見つからなければ不明。"""
    candidates = [request for request in requests
                  if request.status == "approved" and request.content_hash == content_hash
                  and request.origin in SUBMISSION_ORIGINS]
    if not candidates:
        return usage_review.ORIGIN_UNKNOWN
    return max(candidates, key=lambda request: request.id).origin


def transitions_by_name(inputs: ReadInputs) -> dict[str, list[usage_review.Transition]]:
    """配備を実際に変えた成功操作を名前ごとに集める。

    切替は承認・bless が decided で、かつ実際に切り替えたもの (同じ版の bless は含めない)。
    配備操作は rollback が done のもの。"""
    result: dict[str, list[usage_review.Transition]] = {}
    for journal in inputs.journals:
        if (journal.phase == "decided" and journal.kind in ("approve", "bless")
                and journal.switch_required):
            result.setdefault(journal.name, []).append(usage_review.Transition(
                journal.updated_at, str(journal.new_target).rsplit("/", 1)[-1],
                (usage_review.SOURCE_JOURNAL, journal.op_id)))
    for op in inputs.deploy_ops:
        if (op.op_kind == "rollback_symlink" and op.phase == "done"
                and op.target_artifact_hash is not None):
            result.setdefault(op.name, []).append(usage_review.Transition(
                op.updated_at, op.target_artifact_hash, (usage_review.SOURCE_DEPLOY_OP, op.op_id)))
    return result


def _usage_review(inputs: ReadInputs, rows: Sequence[PluginRow], requests: Sequence[_Request],
                  dependents: Mapping[str, list[Dependent]], *, scan_failed: bool,
                  live_unreadable: bool
                  ) -> tuple[usage_review.UsageScan, dict[str, usage_review.UsageFacts]]:
    """読める配備中の指標ごとの事実と、棚卸し全体の事実。"""
    indicators = [row for row in rows
                  if row.kind == KIND_INDICATOR and row.entity.state in (ENTITY_SYMLINK, ENTITY_PLAIN)
                  and row.entity.content_hash is not None]
    live_names = frozenset(row.name for row in indicators)
    records = usage_review.parse_candidates(inputs.candidates, live_names)
    journal_readable = inputs.journal_status != SOURCE_UNAVAILABLE   # 無い DB は記録が無いだけ
    usage_scan = usage_review.build_scan(
        records, candidates_readable=inputs.candidates_status != SOURCE_UNAVAILABLE,
        journal_readable=journal_readable, scan_failed=scan_failed)
    referenced, pending_reason = _pending_condition(inputs, requests)
    transitions = transitions_by_name(inputs)
    requests_by_name: dict[str, list[_Request]] = {}
    for request in requests:
        if request.name is not None:
            requests_by_name.setdefault(request.name, []).append(request)
    facts: dict[str, usage_review.UsageFacts] = {}
    for row in indicators:
        if row.name in referenced:
            pending, reason = usage_review.RESULT_HOLDS, None
        elif pending_reason is not None:
            pending, reason = usage_review.RESULT_UNKNOWN, pending_reason
        else:
            pending, reason = usage_review.RESULT_CLEAR, None
        if journal_readable:
            started, started_reason = usage_review.generation_start(
                transitions.get(row.name, ()), row.entity.artifact_hash)
        else:
            started, started_reason = None, usage_review.REASON_JOURNAL_SOURCE
        facts[row.name] = usage_review.UsageFacts(
            row.name, bool(dependents.get(row.name)), pending, reason, started,
            started_reason,
            _current_origin(requests_by_name.get(row.name, ()), row.entity.content_hash),
            scan_failed, live_unreadable)
    return usage_scan, facts


class ForwardTargetsUnavailable(Exception):
    """配備中の plugin を走査できず、対象の一覧を作れない (正常な空とは別)。"""


def forward_targets(inputs: ReadInputs) -> ForwardTargets:
    """配備中の戦略ごとの前方成績の対象 (名前の順)。世代は棚卸しと同じ投影から取る。

    読める 1 本の strategy の entry は数える。同名が複数ある・strategy なのに link でも
    plain でもない名前は、時刻を持たず理由だけを持つ対象にして (正常な空の一覧と区別する)、
    数値を出す側が「判定不能」にする。live の走査自体が失敗したときは対象を読めないので
    `ForwardTargetsUnavailable` を送出する (呼び出し元が節全体を不明にする)。journal が読めない・遷移が合わない戦略も同じ。
    種別を読めない entry は戦略かどうか決められないので行にせず、数だけ `unreadable_entries`
    に持つ (数値を出す側が「読めない entry があります」と注記する)。"""
    if inputs.live.status == SOURCE_UNAVAILABLE:
        raise ForwardTargetsUnavailable("live plugin scan unavailable")
    by_name: dict[str, list[LiveEntry]] = {}
    unreadable_entries = 0
    for entry in inputs.live.entries:
        if entry.meta is None:
            unreadable_entries += 1
            continue
        by_name.setdefault(entry.meta.name, []).append(entry)
    journal_readable = inputs.journal_status != SOURCE_UNAVAILABLE
    transitions = transitions_by_name(inputs)
    result = []
    for name in sorted(by_name):
        entries = by_name[name]
        strategies = [entry for entry in entries if entry.meta.kind == KIND_STRATEGY]
        if not strategies:
            continue
        if len(entries) > 1 or strategies[0].state not in (ENTITY_SYMLINK, ENTITY_PLAIN):
            result.append(ForwardTarget(name, "", None, None,
                                        usage_review.REASON_STRATEGY_UNREADABLE))
            continue
        entry = strategies[0]
        content = entry.meta.content_hash
        if not journal_readable:
            result.append(ForwardTarget(name, content, None, None,
                                        usage_review.REASON_JOURNAL_SOURCE))
            continue
        started, order, reason = usage_review.generation_of(
            transitions.get(name, ()), entry.artifact_hash)
        result.append(ForwardTarget(name, content, started, order, reason))
    return ForwardTargets(result, unreadable_entries=unreadable_entries)


# ---------------------------------------------------------------- DTO

def _safe_name(name: str) -> str:
    if is_canonical_plugin_name(name):
        return name
    from agentic_fx.loops.approval_facts import display_text
    return display_text(name)


def _hash(value: str | None) -> str | None:
    return value if value is not None and _DISPLAY_HASH.fullmatch(value) else None


def _artifact(value: str | None) -> str | None:
    return value if value is not None and _ARTIFACT_HASH.fullmatch(value) else None


def _usefulness_dto(useful: Usefulness | None) -> dict | None:
    if useful is None:
        return None
    return {"submitted": useful.submitted, "deployed": useful.deployed,
            "by_pin": [{"content_hash": _hash(pin) if pin else None,
                        "submitted": counts[0], "deployed": counts[1]}
                       for pin, counts in useful.by_pin.items()]}


# 棚卸しの scan を渡されなかった行は、候補を調べていないので「当てはまらない」と決めない。
_UNSCANNED = usage_review.UsageScan(candidates_readable=False)


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def row_to_dto(row: PluginRow, *, detail: bool = False,
               usage: usage_review.UsageScan | None = None,
               now: datetime | None = None) -> dict:
    """1 件の JSON 化。値はすべて固定語・数・hash・日時・文法で縛った名前だけ。

    ``usage`` と ``now`` は棚卸しの判定に使う (読む時刻)。"""
    dto = {
        "name": _safe_name(row.name), "kind": row.kind,
        "entity": {"state": row.entity.state,
                   "artifact_hash": _artifact(row.entity.artifact_hash),
                   "content_hash": _hash(row.entity.content_hash),
                   "reason": row.entity.reason},
        "admission": {"state": row.admission.state, "reason": row.admission.reason,
                      "alias": row.admission.alias, "detail": row.admission.detail},
        "pending_count": len(row.pending),
        "pending": [{"id": item.id, "content_hash": _hash(item.content_hash),
                     "kind": item.kind} for item in row.pending],
        "open_ops": [{"source": op.source, "op_id": op.op_id, "kind": op.kind,
                      "target": _hash(op.target)} for op in row.open_ops],
        "issues": list(row.issues),
        "dependencies": [{"alias": item.alias, "plugin": _safe_name(item.plugin),
                          "pin": _hash(item.pin), "state": item.state}
                         for item in sorted(row.deps, key=lambda alias: alias.alias)],
        "dependencies_set_state": row.deps_set_state,
        "dependents": [{"strategy": _safe_name(item.strategy), "alias": item.alias,
                        "pin": _hash(item.pin), "state": item.state}
                       for item in row.dependents],
        "usefulness": _usefulness_dto(row.usefulness),
        "usage_review": (usage_review.classify(row.usage, usage if usage is not None
                                               else _UNSCANNED, _now(now))
                         if row.usage is not None else None),
    }
    if detail:
        dto["history"] = [
            {"id": item.id, "status": item.status, "kind": item.kind,
             "content_hash": _hash(item.content_hash),
             "artifact_hash": _artifact(item.artifact_hash), "origin": item.origin,
             "mission_id": item.mission_id, "decided_at": item.decided_at,
             "created_at": item.created_at} for item in row.history]
        dto["history_total"] = row.history_total
        dto["versions"] = [
            {"artifact_hash": _artifact(item.artifact_hash), "is_live": item.is_live,
             "approval_id": item.approval_id, "status": item.status,
             "content_hash": _hash(item.content_hash)} for item in row.versions]
    return dto


def model_to_dto(model: ReadModel, *, now: datetime | None = None) -> dict:
    when = _now(now)
    return {"sources": dict(model.sources),
            "plugins": [row_to_dto(row, usage=model.usage, now=when) for row in model.rows],
            "invalid_pending": list(model.invalid_pending),
            "unknown_submissions": model.unknown_submissions,
            "usage_inventory": usage_review.window_summary(model.usage, when)}


def detail_to_dto(model: ReadModel, name: str, *, now: datetime | None = None) -> dict | None:
    row = model.by_name(name)
    if row is None:
        return None
    return {"sources": dict(model.sources),
            "plugin": row_to_dto(row, detail=True, usage=model.usage, now=_now(now)),
            "unknown_submissions": model.unknown_submissions}
