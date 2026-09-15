"""承認済み plugin のロード (プラン 7 Task 3、設計書 §6)。

`approval_requests` (`kind="plugin"`) の承認済み行と Task 1 の
`discover`/`content_hash` を突き合わせ、**現在のファイル内容から再算出した
ハッシュが承認時の payload の content_hash と一致する plugin だけ**を返す。
不一致 (承認後の編集) または承認行そのものが無い plugin は「未承認」として
除外し warning ログを出す (fail closed — discover を通っても既定では使われ
ない)。

`kind="plugin"` は `approval_requests.kind` の値 (この承認要求が「plugin の
承認」であることを表す) であり、`PluginMeta.kind` (indicator/signal/strategy
— plugin 自体の種別) とは別の軸。1 つの plugin フォルダには
`kind="plugin"` の承認行が対応し、その payload の中に `PluginMeta.kind` は
含まれない (照合には name + content_hash のみを使う)。

**反映は次回起動時のみ** (hot reload しない — YAGNI、brief の明示指定)。
本番の承認行の書き込み (`approvals.create(kind="plugin", ...)`) は Task 6
の関心。本 task のテストは `approvals.create` + `decide` で承認状態を直接
作る。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_fx.plugin.loader import PluginMeta, discover

if TYPE_CHECKING:
    from agentic_fx.plugin.resolve import InventoryBuildResult, ResolvedIndicatorSet

_log = logging.getLogger(__name__)

# approval_requests.kind の値 (この承認要求の種別)。PluginMeta.kind
# (indicator/signal/strategy) とは別物 — モジュール docstring 参照。
APPROVAL_KIND = "plugin"


def approved_plugins(conn: sqlite3.Connection, plugins_dir: Path, *,
                     settings) -> "InventoryBuildResult":
    """`plugins_dir` を discover し、承認済みかつハッシュ一致の plugin を
    **二相**で admit する ([indicator-consumption-wiring] 設計書 §2.3)。

    第 1 相 = 既存規律 (discover + 最新決定 approved + hash 一致)。
    第 2 相 = strategy を第 1 相の indicator 集合に対して
    `resolve_indicator_deps(pin_mode="require")` に通し、成功したものだけ
    最終 admit する (失敗は warning + reason を log)。

    戻り値は `InventoryBuildResult` — **`list[PluginMeta]` ではない**。
    最終 admit 済の一覧は `result.inventory.metas`、snapshot の材料は
    `result.phase1_metas` (pin 破れ strategy を含む)、prompt の
    「pin 破れ N 本」は `result.rejected_strategies` から作る。
    第 2 相で成功した strategy の `ResolvedIndicatorSet` は
    `result.resolved[(name, content_hash)]` に入る — **消費側は再解決せず
    これをそのまま渡す** (resolver 呼び出しは strategy ごとに 1 回、
    codex r5 I1 / r6 I1)。
    """
    from agentic_fx.plugin.resolve import (
        ApprovedInventory, IndicatorResolutionError, InventoryBuildResult,
        RejectedStrategy, resolve_indicator_deps,
    )
    if not plugins_dir.is_dir():
        _log.info("plugins dir %s does not exist — no plugins loaded", plugins_dir)
        empty = ApprovedInventory(root=plugins_dir, metas=())
        return InventoryBuildResult(inventory=empty, phase1_metas=(),
                                    resolved={}, rejected_strategies=())

    metas = discover(plugins_dir)
    approved_hashes_by_name = _approved_hashes_by_name(conn)

    phase1: list[PluginMeta] = []
    for meta in metas:
        if meta.content_hash in approved_hashes_by_name.get(meta.name, ()):
            phase1.append(meta)
        else:
            _log.warning(
                "plugin %s: 未承認 (承認済みハッシュと不一致、または承認要求が"
                "存在しない) — excluding from load", meta.name)

    root = plugins_dir.resolve()
    indicator_inventory = ApprovedInventory(
        root=root, metas=tuple(m for m in phase1 if m.kind == "indicator"))

    admitted: list[PluginMeta] = []
    resolved: dict[tuple[str, str], "ResolvedIndicatorSet"] = {}
    rejected: list[RejectedStrategy] = []
    for meta in phase1:
        if meta.kind != "strategy":
            admitted.append(meta)
            continue
        try:
            rset = resolve_indicator_deps(
                meta, indicator_inventory, settings=settings, pin_mode="require")
        except IndicatorResolutionError as exc:
            _log.warning(
                "plugin %s: indicator_unresolved:%s:%s — excluding from load",
                meta.name, exc.alias or "-", exc.reason)
            rejected.append(RejectedStrategy(
                name=meta.name, content_hash=meta.content_hash,
                alias=exc.alias, reason=exc.reason))
            continue
        admitted.append(meta)
        resolved[(meta.name, meta.content_hash)] = rset

    return InventoryBuildResult(
        inventory=ApprovedInventory(root=root, metas=tuple(admitted)),
        phase1_metas=tuple(phase1), resolved=resolved,
        rejected_strategies=tuple(rejected))


def _approved_hashes_by_name(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """(name, content_hash) ごとの**最新決定**が approved のものだけを
    admit する (設計書 §7 / D4、プラン9 Task 12)。

    `status IN ('approved','rejected')` かつ `decided_at IS NOT NULL` を
    `ORDER BY decided_at, id` で読み、同じ (name, content_hash) を持つ行を
    順に上書きする — 最後に残った決定が最新の決定になる。同時刻は id が
    タイブレーク (SQL の第二ソートキー)。

    `decided_at` は TEXT (isoformat) なので `ORDER BY` は辞書順比較 —
    書き込み側 (`approvals.decide`) に渡す `now` が常に tz-aware な UTC
    (`SystemClock` の契約) であることに依存する。非 UTC のオフセットが
    混在すると「最新の決定」の判定が時系列と食い違う。

    **`expired` / `invalidated` は決定として数えない** — WHERE 句で最初
    から除外する。どちらも「決定されないまま終端した要求」であり、新しい
    要求の失効が古い承認を取り消すのは誤り (D4)。

    既存の防御 (payload が JSON でない / dict でない / name・content_hash
    が str でない行の warning + skip) はそのまま維持する。
    """
    rows = conn.execute(
        "SELECT id, payload_json, status FROM approval_requests "
        "WHERE kind=? AND status IN ('approved','rejected') "
        "AND decided_at IS NOT NULL "
        "ORDER BY decided_at, id", (APPROVAL_KIND,)).fetchall()
    latest_status: dict[tuple[str, str], str] = {}
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            # DB 破損 (手動編集・移行漏れ等) の観測性のため warning を残す
            # (approved_plugins() 自体は fail closed で継続 — この行は
            # ただ「承認情報として使えない」だけとして扱う)。
            _log.warning(
                "approval_requests id=%s (kind=%s): payload_json が JSON と"
                "して解釈できません (%s) — skipping this row",
                row["id"], APPROVAL_KIND, exc)
            continue
        if not isinstance(payload, dict):
            _log.warning(
                "approval_requests id=%s (kind=%s): payload が dict では"
                "ありません (got %s) — skipping this row",
                row["id"], APPROVAL_KIND, type(payload).__name__)
            continue
        name, content_hash = payload.get("name"), payload.get("content_hash")
        if isinstance(name, str) and isinstance(content_hash, str):
            latest_status[(name, content_hash)] = row["status"]
        else:
            _log.warning(
                "approval_requests id=%s (kind=%s): payload に有効な "
                "name/content_hash (str) がありません — skipping this row",
                row["id"], APPROVAL_KIND)

    out: dict[str, set[str]] = {}
    for (name, content_hash), status in latest_status.items():
        if status == "approved":
            out.setdefault(name, set()).add(content_hash)
    return out
