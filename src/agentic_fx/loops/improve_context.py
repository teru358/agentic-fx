"""改善 Mission への注入コンテキスト生成 (設計書 §3.2)。

親が決定論的に集計してプロンプトへ焼く。各節の出所は設計書 §3.2 の表
どおり: 成績レポート = trade_intents/orders、改善履歴 =
improvement_runs/improvement_backlog/backtest_runs、現行構成インベントリ
= registry/approved_plugins/news_sources/settings、バックログ =
improvement_backlog、ユーザー方針 = Policy.tail、参照 = RunContext
(サンプル plugin コピー・plugin 契約要約・パス・正規形・担当 backlog id)。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentic_fx.policy import Policy, directives_path
from agentic_fx.store import news_sources
from agentic_fx.tools.plugin_loader import approved_plugins

if TYPE_CHECKING:
    import sqlite3
    from agentic_fx.config import Settings

_PLUGIN_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"

# agent 面に出す非提出・降格の分類は固定の 3 語に限る。収益性・取引数・
# 比較不能の区別、他候補の hash / pair / 成績は渡さない。
NOT_SUBMITTED_LAST_RESULT = "observation:not_submitted"
DUPLICATE_LAST_RESULT = "observation:duplicate"
_FAMILY_REDIRECT_PREFIX = "observation:family_redirect:"
# `origin` 列に `not_submitted` を出す `origin_outcome` の値 (`unprofitable` は
# 移行前に書かれた旧値で、同じ語に畳む)。
NOT_SUBMITTED_ORIGINS = frozenset({"not_submitted", "unprofitable"})

# 移行前に書かれた `last_result` のうち、区別を漏らすもの (前方一致)。
_LEGACY_NOT_SUBMITTED_EXACT = frozenset({
    "unprofitable", "observation:unprofitable", "history_source_unconfigured"})
_LEGACY_NOT_SUBMITTED_PREFIX = (
    "insufficient_trades:", "observation:insufficient_trades:",
    "gate_failed:backtest_data_unavailable")
_LEGACY_DUPLICATE = ("duplicate_metrics_of:", "observation:duplicate_metrics_of:")


def family_redirect_last_result(deployed_name: str) -> str:
    """配備済みの同族へ誘導する降格の固定文言 (名前だけ。優劣は含まない)。"""
    return f"observation:{family_redirect_reason(deployed_name)}"


def family_redirect_reason(deployed_name: str) -> str:
    """配備済みの同族へ誘導する、品質検査専用の reason。"""
    return f"family_redirect:{deployed_name}"


_FIXED_LAST_RESULTS = frozenset({
    NOT_SUBMITTED_LAST_RESULT, DUPLICATE_LAST_RESULT, "rejected_by_human",
    "expired", "invalidated", "commit_failed", "interrupted", "reported",
    "human_rejected", "human_noted", "human_reopened"})
# 動的な suffix (承認番号・候補名・hash) を持つ値は、suffix を落として
# この固定の語頭へ畳む。完全一致以外は通さない。
_FOLDED_LAST_RESULT_PREFIXES = (
    ("observation:family_redirect:", "observation:family_redirect"),
    ("gate_failed:noop_copy_of:", "gate_failed:noop_copy"),
    ("approval_pending:", "approval_pending"),
)
_FIXED_LAST_RESULTS = _FIXED_LAST_RESULTS | {
    "unsupported_in_plan10:risk_gate"}
# 自由文が続きうる語頭。語頭だけを残し、後ろは渡さない。
_LAST_RESULT_TAGS = frozenset({
    "gate_failed", "report_failed", "mission_failed", "report", "approved",
    "observation", "unsupported_in_plan10", "approval_pending"})
UNCLASSIFIED_LAST_RESULT = "unclassified"
# 失敗 mission の system note が持つ診断行。呼び出し回数だけを残し、
# 理由・最良候補の成績・archive 位置は渡さない。
_MISSION_DIAGNOSTIC = re.compile(
    r"mission #[0-9]+ status=[a-z_]+ reason=\S* "
    r"run_backtest=([0-9]{1,6}) analyze_corr=([0-9]{1,6})(?: |$)")


def project_last_result(value: str | None, *,
                        deployed_names: frozenset[str] | set[str] = frozenset()
                        ) -> str | None:
    """agent 面に出す `last_result` を固定語彙だけに絞る (fail-closed)。

    移行前の値は区別を畳み、固定表現に一致しない値は語頭だけ、語頭も
    不明なら `unclassified` を返す。数値・自由文・成績は一切通さない。"""
    if value is None:
        return None
    if (value in _LEGACY_NOT_SUBMITTED_EXACT
            or value.startswith(_LEGACY_NOT_SUBMITTED_PREFIX)):
        return NOT_SUBMITTED_LAST_RESULT
    if value.startswith(_LEGACY_DUPLICATE):
        return DUPLICATE_LAST_RESULT
    if value.startswith(_FAMILY_REDIRECT_PREFIX):
        deployed_name = value[len(_FAMILY_REDIRECT_PREFIX):]
        if (re.fullmatch(_PLUGIN_NAME_PATTERN, deployed_name)
                and deployed_name in deployed_names):
            return value
        return "observation:family_redirect"
    diag = _MISSION_DIAGNOSTIC.match(value)
    if diag is not None:
        return (f"mission_diagnostic run_backtest={diag.group(1)} "
                f"analyze_corr={diag.group(2)}")
    base = value.split(" ", 1)[0]
    if base in _FIXED_LAST_RESULTS:
        return base
    for prefix, folded in _FOLDED_LAST_RESULT_PREFIXES:
        if base.startswith(prefix):
            return folded
    tag = base.split(":", 1)[0]
    return tag if tag in _LAST_RESULT_TAGS else UNCLASSIFIED_LAST_RESULT


def _performance_report(conn: "sqlite3.Connection", now: datetime) -> dict:
    # precheck 2026-08-22: T8-m7 -- 未使用の `timezone` import / `since_30`
    # ローカル変数を削除 (window_days=[30, 90] の表示のみで実集計は 90 日窓)。
    since_90 = (now - timedelta(days=90)).isoformat()
    rows_90 = conn.execute(
        "SELECT payload_json, action, gate_result, reject_category, "
        "created_at FROM trade_intents WHERE created_at >= ?",
        (since_90,)).fetchall()
    closed_orders = conn.execute(
        "SELECT pair, realized_pnl, created_at FROM orders "
        "WHERE status='closed' AND created_at >= ?", (since_90,)).fetchall()
    wins = sum(1 for o in closed_orders if (o["realized_pnl"] or 0) > 0)
    total = len(closed_orders)
    gross_profit = sum(o["realized_pnl"] for o in closed_orders
                       if (o["realized_pnl"] or 0) > 0)
    gross_loss = abs(sum(o["realized_pnl"] for o in closed_orders
                         if (o["realized_pnl"] or 0) < 0))
    by_pair: dict[str, dict] = {}
    for o in closed_orders:
        d = by_pair.setdefault(o["pair"], {"count": 0, "pnl": 0.0})
        d["count"] += 1
        d["pnl"] += o["realized_pnl"] or 0
    by_hour: dict[int, int] = {}
    for o in closed_orders:
        hour = datetime.fromisoformat(o["created_at"]).hour
        by_hour[hour] = by_hour.get(hour, 0) + 1
    reject_breakdown: dict[str, int] = {}
    hold_count = 0
    for r in rows_90:
        if r["action"] == "hold":
            hold_count += 1
        if r["gate_result"] == "rejected" and r["reject_category"]:
            reject_breakdown[r["reject_category"]] = (
                reject_breakdown.get(r["reject_category"], 0) + 1)
    return {
        "window_days": [30, 90],
        "win_rate": (wins / total) if total else None,
        "profit_factor": (gross_profit / gross_loss) if gross_loss else None,
        "by_pair": by_pair,
        "by_hour": by_hour,
        "reject_breakdown": reject_breakdown,
        "hold_rate": (hold_count / len(rows_90)) if rows_90 else None,
    }


def _improvement_history(conn: "sqlite3.Connection", *,
                         deployed_names: frozenset[str] = frozenset()) -> dict:
    rows = conn.execute(
        "SELECT ir.id, ir.backlog_id, ir.result, ir.started_at, ir.finished_at, "
        "ib.idea, ib.attempts, ib.last_result "
        "FROM improvement_runs ir LEFT JOIN improvement_backlog ib "
        "ON ib.id = ir.backlog_id ORDER BY ir.id DESC LIMIT 50").fetchall()
    runs = [dict(r) for r in rows]
    for r in runs:
        r["last_result"] = project_last_result(
            r["last_result"], deployed_names=deployed_names)
    return {"recent_runs": runs}


def deployed_families(metas: "list[Any]") -> list[dict]:
    """配備済み plugin の仮説族 (名前と構成だけ。成績は含まない)。

    strategy は依存 indicator の組・足・pair、indicator は名前と足。"""
    out = []
    for m in metas:
        out.append({
            "name": m.name, "kind": m.kind,
            "timeframe": m.timeframe,
            "pairs": list(m.pairs) if m.kind == "strategy" else [],
            "indicators": (sorted({r.plugin for r in m.indicators})
                           if m.kind == "strategy" else [])})
    return sorted(out, key=lambda f: (f["kind"], f["name"]))


def deployed_families_text(families: list[dict]) -> str:
    """prompt に差し込む仮説族の一覧。名前と構成のみで、成績は含まない。"""
    if not families:
        return "(なし)"
    lines = []
    for f in families:
        timeframe = f["timeframe"] or "-"
        if f["kind"] == "strategy":
            indicators = ", ".join(f["indicators"]) or "(なし)"
            pairs = ", ".join(f["pairs"]) or "-"
            lines.append(f"- {f['name']} (strategy): indicators=[{indicators}], "
                         f"timeframe={timeframe}, pairs={pairs}")
        else:
            lines.append(f"- {f['name']} (indicator): timeframe={timeframe}")
    return "\n".join(lines)


def _current_inventory(conn: "sqlite3.Connection", settings: "Settings",
                       root: Path, *,
                       inventory_view: dict | None = None,
                       inventory_metas: "list[Any] | None" = None) -> dict:
    """[indicator-consumption-wiring] §2.9(a): prompt の
    `current_inventory.approved_plugins` は **mission の inventory view**
    から作る (prepare 後に live `plugins/` を差し替えても不変、P3)。
    `inventory_view` が渡されない経路 (CLI 表示等) は従来どおり
    `approved_plugins` を呼ぶ。

    載せるのは name / kind / pairs / params / outputs / content_hash のみ —
    成績・期間・段名は載せない (遮断 8)。"""
    if inventory_view is not None:
        plugin_summaries = [
            {"name": p["name"], "kind": p["kind"], "pairs": p["pairs"],
             "params": p["params"], "outputs": p["outputs"],
             "content_hash": p["content_hash"]}
            for p in inventory_view["plugins"]]
    else:
        plugins = approved_plugins(conn, root / "plugins",
                                   settings=settings).inventory.metas
        if inventory_metas is None:
            inventory_metas = list(plugins)
        plugin_summaries = [
            {"name": p.name, "kind": p.kind, "pairs": list(p.pairs),
             "params": p.params,
             "outputs": (list(p.outputs) if p.outputs is not None else None),
             "content_hash": p.content_hash} for p in plugins]
    sources = news_sources.list_all(conn)
    return {
        "approved_plugins": plugin_summaries,
        "deployed_families": deployed_families(inventory_metas or []),
        "news_sources": [{"name": s["name"], "enabled": s["enabled"]}
                         for s in sources],
        "risk_gate": settings.risk.model_dump(),
    }


def _backlog_section(conn: "sqlite3.Connection",
                     allowed_backlog_ids: "frozenset[int] | None", *,
                     deployed_names: frozenset[str]) -> dict:
    from agentic_fx.store import backlog as backlog_mod
    items = []
    for row in backlog_mod.list_open(conn):
        item = {"id": row["id"], "idea": row["idea"], "status": row["status"],
                "attempts": row["attempts"],
                "last_result": project_last_result(
                    row["last_result"], deployed_names=deployed_names),
                "origin_outcome": row["origin_outcome"]}
        if allowed_backlog_ids is not None:
            item["assigned"] = row["id"] in allowed_backlog_ids
        items.append(item)
    all_notes = list(reversed(backlog_mod.list_notes(conn)))
    notes = [
        {"id": row["id"], "idea": row["idea"], "status": row["status"],
         "attempts": row["attempts"],
         "last_result": project_last_result(
             row["last_result"], deployed_names=deployed_names),
         "origin_outcome": row["origin_outcome"]}
        for row in all_notes[:20]
    ]
    return {"items": items, "notes": notes,
            "notes_omitted": max(0, len(all_notes) - len(notes))}


def _user_policy_section(root: Path) -> dict:
    policy = Policy(directives_path(root))
    return {"tail": policy.tail(4000)}


def _references_section() -> dict:
    return {
        "plugin_name_pattern": _PLUGIN_NAME_PATTERN,
        "plugin_contract_summary": (
            "plugin.py / config.yaml / test_plugin.py の 3 本。kind は "
            "indicator|signal|strategy。config.yaml は既存 _validate_config "
            "の検証を通る形式。"),
    }


def build_improve_context(
        conn: "sqlite3.Connection", *, settings: "Settings", now: datetime,
        root: Path,
        allowed_backlog_ids: "frozenset[int] | None",
        inventory_view: dict | None = None,
        inventory_metas: "list[Any] | None" = None) -> dict[str, Any]:
    current_inventory = _current_inventory(
        conn, settings, root, inventory_view=inventory_view,
        inventory_metas=inventory_metas)
    deployed_names = frozenset(
        family["name"] for family in current_inventory["deployed_families"])
    return {
        "performance_report": _performance_report(conn, now),
        "improvement_history": _improvement_history(
            conn, deployed_names=deployed_names),
        "current_inventory": current_inventory,
        "backlog": _backlog_section(
            conn, allowed_backlog_ids, deployed_names=deployed_names),
        "user_policy": _user_policy_section(root),
        "references": _references_section(),
    }
