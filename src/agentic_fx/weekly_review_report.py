"""週次の評価の report (親専有)。確定した記録だけから作るので、書込みに失敗しても
同じ確定から作り直せる。data/ 配下の専用 dir に owner-only で 1 週 1 file。agent の入力・
RAG・取引 prompt には結ばない。"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from agentic_fx.loops import econ_signal_report as econ
from agentic_fx.plugin.loader import is_canonical_plugin_name
from agentic_fx.store import weekly_review as store

REPORT_DIR_NAME = "weekly-review"
_PART_NAME = re.compile(r"\.weekly-review-.+\.part\Z")

_NOTE = ("機械採点は、採否できた signal の採否時点からの仮想成績で、実約定を含まず楽観側に出ます。"
         "採否前に abandoned となった signal は含みません (穴として件数だけ数えます)。"
         "「床超え」は機械採点上の意味に限り、実運用の収益性を保証しません。")

_STATUS = {"completed": "完了", "aborted": "打切り"}
_EVALUATION = {"normal": "正常", "degraded": "劣化", "unevaluated": "未評価"}
_SIGN = {"indeterminate": "判定不能", "insufficient": "標本不足", "stop_candidate": "停止候補",
         "near_zero": "ゼロ近傍"}
_REASON = {
    "no_recent_decisions": "直近 2 週に採否のある signal がない",
    "overdue_unterminated": "評価時刻が期限を超えた未終端がある",
    "deadline_unknown": "期限を読めない未終端がある",
    "generation_aged_insufficient": "現世代が 12 週に達しても N が 30 に届かない"}
_DEGRADED = {
    "strategy_undetermined": "判定不能の戦略", "strategy_failed": "集計に失敗した戦略",
    "inventory_unavailable": "指標の棚卸しが判定不能", "targets_unavailable":
    "窓終端時点の対象を復元できなかった"}
_NOT_PROPOSABLE = {"human_origin": "人間由来", "unknown_origin": "出所不明"}
_INVENTORY_STATE = {"unused": "未使用", "indeterminate": "判定不能"}


def _name(value: object) -> str:
    if isinstance(value, str) and is_canonical_plugin_name(value):
        return value
    return "(名前を表示できません)"


def _verdict(row: dict) -> str:
    sign, reasons = row["sign"], row["reasons"]
    if sign == "near_zero" or (sign == "above_floor" and reasons):
        return "要確認"
    if sign == "above_floor":
        return "機械採点上は床超え"
    return _SIGN[sign]


def _strategy_lines(row: dict) -> list[str]:
    lines = ["", f"### {_name(row['name'])} ({row['content_hash'][:12]})"]
    if row["state"] != "ok":
        reason = row["state_reason"] or "-"
        lines.append("集計できませんでした" if row["state"] == "failed"
                     else f"判定不能 ({reason})")
        return lines
    series, holes, dec = row["series"], row["holes"], row["decisions"]
    lines.append(f"- 判定: {_verdict(row)}"
                 + (f" (符号: {_SIGN.get(row['sign'], '床超え候補')})"
                    if row["sign"] in ("near_zero", "above_floor") and row["reasons"] else ""))
    for reason in row["reasons"]:
        lines.append(f"  - 要確認の理由: {_REASON.get(reason, reason)}")
    lines += [
        f"- 世代: 開始 {row['generation_started_at']} / 最初の採否 {row['first_decided_at']}",
        f"- 全採否: {econ._forward_values(series['all'])}",
        f"- accept: {econ._forward_values(series['accept'])}",
        f"- reject: {econ._forward_values(series['reject'])}"]
    aband = ", ".join(f"{k}={v}" for k, v in holes.get("abandoned", {}).items()) or "なし"
    oldest = holes.get("oldest_unterminated_sec")
    lines += [
        f"- 穴: abandoned: {aband} / no_fill={holes.get('no_fill', 0)} / "
        f"data_insufficient={holes.get('data_insufficient', 0)} / "
        f"未終端={holes.get('unterminated', 0)}"
        f" (最古 {'–' if oldest is None else f'{oldest / 3600:.1f} 時間'}、"
        f"期限超え {holes.get('overdue_unterminated', 0)}、"
        f"期限不明 {holes.get('deadline_unknown', 0)}) / "
        f"時点不明={holes.get('as_of_unknown', 0)} / R不明={holes.get('r_unknown', 0)} / "
        f"時刻不明={holes.get('time_unreadable', 0)}",
        f"- 採否: accept={dec.get('accept', 0)} reject={dec.get('reject', 0)} "
        f"risk gate 通過={dec.get('gate_passed', 0)} / signal 到達率="
        f"{econ._forward_rate(dec['reach'])}",
        f"- 集中度: 採点のある取引日={row['concentration']['days']} / 日別最大="
        f"{row['concentration']['max_day_count']} 件 "
        f"({econ._percent(row['concentration']['max_day_share'])})"]
    short = row["short_term"]
    avg = short.get("avg_r")
    lines.append(f"- 直近 {short.get('weeks', 4)} 週 (表示のみ): N={short.get('n', 0)} 平均R="
                 f"{'–' if avg is None else f'{avg:.2f}'}")
    comp = short.get("comparison")
    if comp is not None:
        lines.append(f"- 到達率の比較 (表示のみ): 直近 4 週 {econ._forward_rate(comp['recent'])}"
                     f" / 前 4 週 {econ._forward_rate(comp['previous'])}"
                     + ("" if comp["comparable"] else " (比較不能)"))
    approval = row["approval"]
    if approval:
        parts = [f"{pair} 件数={vals.get('trades', '–')} PF={vals.get('pf', '–')} "
                 f"平均R={vals.get('avg_r', '–')}" for pair, vals in approval["pairs"].items()]
        lines.append("- 承認時の成績 (判定には使いません): " + " / ".join(parts))
    return lines


def _inventory_lines(week: dict, items: list[dict]) -> list[str]:
    lines = ["", "## 未使用の指標 (棚卸し)"]
    if week["inventory_state"] != store.INVENTORY_NORMAL:
        lines.append(f"棚卸しは判定不能です ({week['inventory_reason'] or '-'})")
        if not items:
            return lines
    first = week.get("_first_inventory", False)
    if not items:
        lines.append("なし")
        return lines
    for item in items:
        mark = []
        if item["state"] == "indeterminate":
            mark.append("判定不能")
        elif not item["proposable"]:
            mark.append("提案対象外 (" + _NOT_PROPOSABLE.get(item["not_proposable"], "出所不明")
                        + ")")
        if item["is_new"] and not first:
            mark.append("新規")
        lines.append(f"- {_name(item['name'])}: {_INVENTORY_STATE[item['state']]}"
                     + (f" [{' / '.join(mark)}]" if mark else ""))
    return lines


def render_report(conn: sqlite3.Connection, label: str) -> str | None:
    """確定した週の report 本文。記録が無ければ None。"""
    week = store.get_week(conn, label)
    if week is None:
        return None
    lines = [f"# 週次の稼働中戦略の評価 {label}", "",
             f"- 窓終端: {week['window_end']}",
             f"- 確定: {week['finalized_at']}",
             f"- 評価時刻: {week['evaluated_at'] or '–'}",
             f"- 確定状態: {_STATUS[week['status']]}",
             f"- 評価状態: {_EVALUATION[week['evaluation']]}", "", _NOTE]
    if week["status"] == store.STATUS_ABORTED:
        lines += ["", "確定できないまま集計の期間 (窓終端から 14 日) を過ぎたため打切りにしました "
                  "(期間超過)。この週の集計・判定・棚卸しはありません。"]
        return "\n".join(lines) + "\n"
    for reason, count in sorted(week["degraded"].items()):
        lines.append(f"- 劣化の理由: {_DEGRADED.get(reason, reason)} ({count})")
    lines += ["", "## 戦略別"]
    strategies = store.strategies_of(conn, label)
    if not strategies:
        lines.append("配備中の戦略はありませんでした")
    for row in strategies:
        lines += _strategy_lines(row)
    items = store.inventory_of(conn, label)
    week["_first_inventory"] = conn.execute(
        "SELECT 1 FROM review_weeks WHERE status='completed' AND inventory_state='normal' "
        "AND week<?", (label,)).fetchone() is None
    lines += _inventory_lines(week, items)
    lines += ["", "## 停止済み"]
    stopped = week["stopped"]
    lines += ([f"- {_name(item.get('name'))} ({item.get('kind') or '-'})" for item in stopped]
              or ["なし"])
    return "\n".join(lines) + "\n"


def report_name(label: str) -> str:
    return f"weekly-review-{label}.md"


def ensure_report(conn: sqlite3.Connection, label: str, *, reports_dir: Path,
                  anchor: Path | None = None) -> Path | None:
    """report が無ければ記録から作る。作ったら path、既にある・記録が無ければ None。"""
    name = report_name(label)
    if econ.named_report_exists(reports_dir, name=name, anchor=anchor):
        return None
    body = render_report(conn, label)
    if body is None:
        return None
    return econ.write_named_once(reports_dir, name=name,
                                 part_prefix=f".weekly-review-{label}", body=body,
                                 anchor=anchor, part_pattern=_PART_NAME)
