"""改善 Mission への注入コンテキスト生成 (設計書 §3.2)。

親が決定論的に集計してプロンプトへ焼く。各節の出所は設計書 §3.2 の表
どおり: 成績レポート = trade_intents/orders、改善履歴 =
improvement_runs/improvement_backlog/backtest_runs、現行構成インベントリ
= registry/approved_plugins/news_sources/settings、バックログ =
improvement_backlog、ユーザー方針 = Policy.tail、参照 = RunContext
(サンプル plugin コピー・plugin 契約要約・パス・正規形・担当 backlog id)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentic_fx.policy import Policy
from agentic_fx.store import news_sources
from agentic_fx.tools.plugin_loader import approved_plugins

if TYPE_CHECKING:
    import sqlite3
    from agentic_fx.config import Settings

_PLUGIN_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


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


def _improvement_history(conn: "sqlite3.Connection") -> dict:
    rows = conn.execute(
        "SELECT ir.id, ir.backlog_id, ir.result, ir.started_at, ir.finished_at, "
        "ib.idea, ib.attempts, ib.last_result "
        "FROM improvement_runs ir LEFT JOIN improvement_backlog ib "
        "ON ib.id = ir.backlog_id ORDER BY ir.id DESC LIMIT 50").fetchall()
    return {"recent_runs": [dict(r) for r in rows]}


def _current_inventory(conn: "sqlite3.Connection", settings: "Settings",
                       root: Path) -> dict:
    plugins_dir = root / "plugins"
    plugins = approved_plugins(conn, plugins_dir)
    plugin_summaries = [
        {"name": p.name, "kind": p.kind, "pairs": p.pairs} for p in plugins]
    sources = news_sources.list_all(conn)
    return {
        "approved_plugins": plugin_summaries,
        "news_sources": [{"name": s["name"], "enabled": s["enabled"]}
                         for s in sources],
        "risk_gate": settings.risk.model_dump(),
    }


def _backlog_section(conn: "sqlite3.Connection",
                     allowed_backlog_ids: "frozenset[int] | None") -> dict:
    from agentic_fx.store import backlog as backlog_mod
    items = []
    for row in backlog_mod.list_open(conn):
        item = {"id": row["id"], "idea": row["idea"], "status": row["status"],
                "attempts": row["attempts"], "last_result": row["last_result"]}
        if allowed_backlog_ids is not None:
            item["assigned"] = row["id"] in allowed_backlog_ids
        items.append(item)
    notes = [
        {"id": row["id"], "idea": row["idea"], "status": row["status"],
         "attempts": row["attempts"], "last_result": row["last_result"]}
        for row in backlog_mod.list_notes(conn)
    ]
    return {"items": items, "notes": notes}


def _user_policy_section(root: Path) -> dict:
    policy = Policy(root / "policy" / "directives.md")
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
        allowed_backlog_ids: "frozenset[int] | None") -> dict[str, Any]:
    return {
        "performance_report": _performance_report(conn, now),
        "improvement_history": _improvement_history(conn),
        "current_inventory": _current_inventory(conn, settings, root),
        "backlog": _backlog_section(conn, allowed_backlog_ids),
        "user_policy": _user_policy_section(root),
        "references": _references_section(),
    }
