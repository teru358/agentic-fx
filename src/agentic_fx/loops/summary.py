"""状態サマリ (常時注入) と Mission 出力スキーマ — 設計書 §5。決定論的コードが生成。"""
from __future__ import annotations

import copy
import sqlite3
from datetime import datetime

from agentic_fx.core.contracts import Clock, OrderStatus
from agentic_fx.core.paper_broker import PaperBroker
from agentic_fx.core.accounting import daily_start_equity
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.store import orders

TRADE_INTENT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "action": {"enum": ["open", "close", "cancel", "hold"]},
        "pair": {"type": "string"},
        "direction": {"enum": ["long", "short"]},
        "entry_type": {"enum": ["market", "limit"]},
        "horizon": {"enum": ["day", "swing"]},
        "order_id": {"type": "integer"},
        "limit_price": {"type": ["number", "null"]},
        "expires_in": {"type": ["string", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "take_profit": {"type": ["number", "null"]},
        "confidence": {"type": ["number", "null"]},
        "reasoning": {"type": "string"},
    },
    "required": ["action", "reasoning"],
}

ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
}

_ACTIVE = (OrderStatus.OPEN, OrderStatus.PENDING_FILL,
           OrderStatus.PROTECTION_PENDING)


def trade_intent_schema(pairs: list[str]) -> dict:
    """TRADE_INTENT_SCHEMA の pair を settings.pairs の enum に絞った版。

    最終防衛は TradeIntent.from_llm_dict + executor — enum は LLM への誘導と
    早期拒否 (schema リトライで LLM に「pairs 外」を伝える) であり、
    防衛線の置き換えではない。
    """
    schema = copy.deepcopy(TRADE_INTENT_SCHEMA)
    schema["properties"]["pair"] = {"enum": list(pairs)}
    return schema


def build_state_summary(conn: sqlite3.Connection, broker: PaperBroker,
                        econ: EconCalendar, clock: Clock,
                        starting_balance: float) -> str:
    now = clock.now()
    balance, equity = broker.equity()
    day_start = daily_start_equity(conn, now)
    daily_pnl = (equity - day_start) if day_start is not None else 0.0
    total_pnl = equity - starting_balance

    lines = ["## 現在の状態 (システム生成)",
             f"- 時刻: {now.isoformat()}",
             f"- 残高: {balance:,.0f} / エクイティ: {equity:,.0f}",
             f"- 累計損益: {total_pnl:+,.0f} / 日次損益: {daily_pnl:+,.0f}"]

    active = orders.list_by_status(conn, *_ACTIVE)
    if active:
        lines.append("- ポジション/指値:")
        for r in active:
            entry = r["avg_fill_price"] or r["requested_price"]
            lines.append(
                f"  - #{r['id']} {r['pair']} {r['direction']} "
                f"{r['status']} {r['quantity']}lot @{entry} "
                f"SL={r['stop_loss']} TP={r['take_profit']} "
                f"horizon={r['horizon']}")
    else:
        lines.append("- ポジション/指値: なし")

    closed = conn.execute(
        "SELECT * FROM orders WHERE status='closed' "
        "ORDER BY closed_at IS NULL ASC, closed_at DESC, id DESC LIMIT 10").fetchall()
    if closed:
        lines.append("- 直近トレード:")
        for r in closed:
            pnl_str = (f"{r['realized_pnl']:+,.0f}"
                      if r['realized_pnl'] is not None
                      else "未確定")
            lines.append(
                f"  - #{r['id']} {r['pair']} {r['direction']} "
                f"pnl={pnl_str} ({r['close_reason']})")

    events = econ.upcoming(hours=24)
    if events:
        lines.append("- 24h 以内の経済指標:")
        for e in events[:8]:
            stars = "★" * int(e["importance"] or 0)
            lines.append(f"  - {e['ts']} {e['country']} {e['name']} {stars}")
    return "\n".join(lines)


IMPROVE_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "required": ["discoveries", "selected", "artifact", "selection_rationale"],
    # I3: `additionalProperties: false` — §3.5「analysis ID・回数を agent
    # に申告させない」を schema 境界として強制する (未知キーの列挙・混入
    # を top-level・discoveries[]・selected・artifact の全 5 object で拒否)。
    "additionalProperties": False,
    "properties": {
        "discoveries": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["idea", "source", "evidence", "kind"],
                "additionalProperties": False,
                "properties": {
                    "idea": {"type": "string"},
                    "source": {"type": "string", "enum": ["agent", "research"]},
                    "evidence": {"type": "string"},
                    "kind": {"type": "string", "enum": ["task", "fact"]},
                },
            },
        },
        "selected": {
            "type": "object",
            "required": ["backlog_id", "idea"],
            "additionalProperties": False,
            "properties": {
                "backlog_id": {"type": ["integer", "null"]},
                # C14 裁定 (2026-08-28、束D検収 verified-local-round1.md §7):
                # 空文字は schema 層でも拒否する。空白のみ ("   ") は
                # `minLength` では捕まえられない — コード層 (D18 是正、
                # `_select_and_bind` の `.strip()` fail closed) が引き続き
                # 担う。2 層の防御を意図的に重ねる (schema 層は明白な空、
                # コード層は空白のみも含めた広い判定)。
                "idea": {"type": "string", "minLength": 1},
            },
        },
        "artifact": {
            # 実機実測 (2026-08-30, runbook): ChatGPT structured outputs は
            # oneOf 不許可 + 全 property に type 必須。3 variant は const 判別で
            # 排他なので anyOf は oneOf と同値。
            "anyOf": [
                {
                    "type": "object",
                    "required": ["type", "name", "kind", "self_test", "summary"],
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "const": "plugin"},
                        "name": {"type": "string",
                                 "pattern": "^[a-z][a-z0-9_]{0,63}$"},
                        "kind": {"type": "string",
                                "enum": ["indicator", "signal", "strategy"]},
                        "self_test": {"type": "string",
                                     "enum": ["passed", "failed", "not_run"]},
                        "summary": {"type": "string"},
                    },
                },
                {
                    "type": "object",
                    "required": ["type", "proposal_kind", "title", "body_md"],
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "const": "report"},
                        "proposal_kind": {"type": "string",
                                         "enum": ["core", "risk_gate", "research"]},
                        "title": {"type": "string"},
                        "body_md": {"type": "string"},
                    },
                },
                {
                    "type": "object",
                    "required": ["type", "reason"],
                    "additionalProperties": False,
                    "properties": {
                        "type": {"type": "string", "const": "observation"},
                        "reason": {"type": "string"},
                    },
                },
            ],
        },
        "selection_rationale": {"type": "string"},
    },
}
