"""改善 mission の一覧 (/improve) と詳細 (/improve/{id}) の画面。

daemon の応答のうち、ここで名指しした field だけを表示する。語は固定表で日本語に写し、
agent の自由文 (仮説・説明・観察文) は除去 → escape → 上限の順で引用として出す。
"""
from __future__ import annotations

import html
from collections.abc import Mapping

from afx_ops.display_zone import zone_of
from afx_ops.plugin_text import label
from afx_ops.web import render
from afx_ops.web import plugin_pages
from afx_ops.web.plugin_pages import _pick, _plugin_link, _time
from afx_ops.web.routes import resolve_route

LIST_FIELDS = frozenset({"runs", "has_more"})
RUN_FIELDS = frozenset({"id", "started_at", "finished_at", "trigger", "result_code",
                        "reason_code", "plugin", "failed_stage", "failed_conditions",
                        "failed_pair_count", "approval_id"})
DETAIL_FIELDS = frozenset({"id", "backend", "model", "elapsed_sec", "snapshot", "report_state",
                           "approval", "floor", "summary"})
SNAPSHOT_FIELDS = frozenset({"trigger", "result_code", "reason_code", "tool_calls",
                             "observation_text"})
APPROVAL_FIELDS = frozenset({"id", "plugin", "kind"})
SUMMARY_FIELDS = frozenset({"version", "state", "items"})
ITEM_FIELDS = frozenset({"hypothesis", "explanation", "observation", "parent_reason", "tried",
                         "floor", "comparison", "handoff"})
HYPOTHESIS_FIELDS = frozenset({"idea", "backlog_id"})
EXPLANATION_FIELDS = frozenset({"rationale", "artifact_summary"})
TRIED_FIELDS = frozenset({"plugin", "kind", "origin", "indicators"})
FLOOR_FIELDS = frozenset({"thresholds", "failed_stage", "failed_conditions",
                          "failed_pair_count", "stages"})
THRESHOLD_FIELDS = frozenset({"min_pf", "require_positive_avg_r", "require_holdout_evaluable",
                              "max_drawdown", "max_trades_per_eval_bar"})
STAGE_FIELDS = frozenset({"stage", "state", "pairs"})
PAIR_FIELDS = frozenset({"pair", "trades", "pf", "avg_r", "max_drawdown", "trades_per_eval_bar",
                         "evaluable", "failed"})
COMPARISON_FIELDS = frozenset({"mode", "verdict", "reason", "stage", "metrics", "pairs"})
COMPARISON_PAIR_FIELDS = frozenset({"pair", "verdict", "candidate", "deployed"})
SIDE_FIELDS = frozenset({"trades", "pf", "avg_r", "evaluable"})
HANDOFF_FIELDS = frozenset({"family", "attempts", "outcomes"})
REPORT_FLOOR_FIELDS = frozenset({"stages"})
REPORT_PAIR_FIELDS = frozenset({"pair", "trades", "pf", "avg_r", "evaluable", "conditions"})

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
TEXT_CHARS = 500
FAMILY_CHARS = 160
ROW_LIMIT = 200
CELL_ITEMS = 5
# 詳細の節ごとの byte の上限 (合計と枠・nav・style が 64 KiB に収まる値)
SECTION_BUDGET = {"floor": 14 * 1024, "comparison": 12 * 1024, "tried": 3 * 1024,
                  "handoff": 3 * 1024}
OMIT_CLI = "全件は afx ctl improve runs で確認してください"

TRIGGER = {"manual": "手動", "scheduled": "週次"}
RESULT = {"submitted": "候補を提出", "observation": "提出なし", "gate_failed": "見送り",
          "failed": "失敗", "report": "レポートを公開"}
REASON = {
    "not_submitted": "agent の申告なし", "demoted_recovered": "回収経由のため降格",
    "risk_gate_unsupported": "risk_gate は未対応", "duplicate_logic": "既存候補と同じロジック",
    "duplicate_metrics": "既存候補と同じ成績", "family_redirect": "既存系統への誘導",
    "duplicate_skipped": "重複のため省略", "unprofitable": "床未達",
    "insufficient_trades": "取引数が不足", "history_source_unconfigured": "履歴の取得元が未設定",
    "backtest_data_unavailable": "バックテストの履歴が使えない",
    "history_not_ready": "履歴条件を満たさない", "comparison_base_unavailable": "比較元を取れない",
    "not_improved": "配備中の版を上回らない", "same_name_contract_changed": "同名更新の条件変更",
    "snapshot_rejected": "提出物を検査で拒否", "source_rejected": "ソースを検査で拒否",
    "loader_rejected": "loader が拒否", "syntax_error": "構文エラー",
    "noop_copy": "変更なしの複製", "self_test_missing": "自己 test が無い",
    "pytest_failed": "自己 test が失敗", "self_test_too_thin": "自己 test が薄い",
    "self_test_mutated": "自己 test が変異を見逃した", "unverified": "検証できず",
    "max_bars_limit": "max_bars が上限超過", "kind_unsupported": "未対応の kind",
    "pair_not_tradable": "取引できない pair", "outputs_required": "出力の宣言が必要",
    "indicator_unresolved": "indicator を解決できない", "symbol_in_name": "名前に銘柄を含む",
    "param_price_unit": "価格単位の param", "fixture_run_failed": "fixture の実行に失敗",
    "output_not_finite": "出力が有限でない", "output_constant": "出力が定数",
    "indicator_duplicate": "indicator が重複",
    "indicator_monotone_duplicate": "単調変換の重複 indicator",
    "gate_sandbox_unavailable": "gate の隔離を使えない",
    "report_write_failed": "レポートの書込みに失敗", "sandbox_unavailable": "隔離を使えない",
    "timeout": "時間切れ", "max_turns": "turn 数の上限", "runner_failed": "runner が失敗",
    "tool_budget_abort": "tool 予算で中断", "output_invalid": "出力が不正",
    "approval_tx_failed": "承認依頼の保存に失敗", "commit_crashed": "確定中に停止",
    "launch_crashed": "起動中に停止", "prepare_failed": "準備に失敗", "interrupted": "中断",
}
REPORT_STATE = {"none": "なし", "prepared": "準備済み", "published": "公開済み", "failed": "失敗"}
SUMMARY_STATE = {"ok": "あり", "empty": "材料なし", "build_failed": "作成に失敗"}
KIND = {"indicator": "indicator", "signal": "signal", "strategy": "strategy"}
ORIGIN = {"new": "新規", "update": "更新"}
STAGE = {"in_sample": "in-sample", "holdout": "holdout"}
STAGE_STATE = {"measured": "測定", "reference": "参考", "not_run": "未実行"}
CONDITION = {"pf": "PF", "avg_r": "平均 R", "holdout_not_evaluable": "holdout を評価できない",
             "drawdown_too_deep": "DD が深すぎる", "too_frequent": "取引頻度が高すぎる"}
COMPARISON_VERDICT = {"improved": "上回った", "not_improved": "上回らない",
                      "unavailable": "比較できない"}
COMPARISON_REASON = {
    "holdout_exceeds": "holdout で配備中の版を上回る",
    "holdout_not_exceeds": "holdout で配備中の版を上回らない",
    "baseline_not_evaluable": "配備中の版を評価できない",
    "candidate_not_evaluable": "候補を評価できない", "no_pairs": "比較できる pair が無い",
    "gate_timeout": "比較が時間切れ", "baseline_unhealthy": "配備中の版が不健全",
}
COMPARISON_MODE = {"shadow": "shadow (参考)", "enforce": "enforce (採否に反映)"}
METRIC = {"pf": "PF", "avg_r": "平均 R"}
NO_SUMMARY_TEXT = "要約はありません (要約の導入前の run、または回収中の run です)"
NO_TERMINAL_TEXT = "終端の記録がありません (実行中、または記録前の mission です)"
SUMMARY_NOTES = {"empty": "要約の材料がありません", "build_failed": "要約を組み立てられませんでした"}


def _text(value: object, word: dict[str, str] | None = None) -> str:
    if word is not None:
        return html.escape(label(word, value), quote=True) if value is not None else "-"
    return render._text(value) or "-"


def _num(value: object) -> str:
    return render._num(value) if type(value) in (int, float) else "-"


def _bool(value: object) -> str:
    return {True: "はい", False: "いいえ"}.get(value, "-") if type(value) is bool else "-"


def _quote(value: object, limit: int = TEXT_CHARS) -> str:
    shown = render._cut(value, limit, block=True)
    return f"<pre>{shown}</pre>" if shown else "<p>(記載なし)</p>"


def _section(title: str, body: str) -> str:
    return f"<section><h2>{title}</h2>{body}</section>"


def _table(headers: tuple[str, ...], rows: list[list[str]], budget: int, hidden: int = 0) -> str:
    return plugin_pages._table(headers, rows, budget, hidden, cli=OMIT_CLI)


def _list_of(value: object, allowed: frozenset[str]) -> tuple[list[dict], int]:
    """list の dict を allowlist で絞る。ROW_LIMIT を超える分は整形せず件数だけ返す。"""
    if type(value) is not list:
        return [], 0
    shown = [_pick(item, allowed) for item in value[:ROW_LIMIT] if type(item) is dict]
    return shown, max(0, len(value) - ROW_LIMIT)


def _conditions(value: object) -> str:
    if type(value) is not list or not value:
        return "-"
    shown = [html.escape(label(CONDITION, word), quote=True) for word in value[:CELL_ITEMS]]
    omitted = len(value) - len(shown)
    if omitted > 0:
        shown.append(f"ほか {omitted} 件")
    return " / ".join(shown)


def _approval_link(approval_id: object) -> str:
    if type(approval_id) is int and approval_id >= 1:
        if resolve_route(f"/approvals/{approval_id}") is not None:
            return f'<a href="/approvals/{approval_id}">#{approval_id}</a>'
    return "-"


def _run_link(run_id: object) -> str:
    if type(run_id) is int and resolve_route(f"/improve/{run_id}") is not None:
        return f'<a href="/improve/{run_id}">#{run_id}</a>'
    return "-"


def _result_text(result: object, reason: object) -> str:
    text = label(RESULT, result)
    if reason is not None:
        text += f" ({label(REASON, reason)})"
    return html.escape(text, quote=True)


# ---------------------------------------------------------------- 一覧

LIST_HEADERS = ("run", "開始", "終了", "起動", "結果", "対象", "床を割った箇所", "承認")


def _floor_cell(row: dict) -> str:
    stage = row.get("failed_stage")
    if stage is None:
        return "-"
    text = html.escape(label(STAGE, stage), quote=True) + ": " + _conditions(
        row.get("failed_conditions"))
    count = row.get("failed_pair_count")
    if type(count) is int:
        text += f" ({count} pair)"
    return text


def _list_row(raw: dict, tz: str) -> list[str]:
    row = _pick(raw, RUN_FIELDS)
    plugin = row.get("plugin")
    return [_run_link(row.get("id")), _time(row.get("started_at"), tz),
            _time(row.get("finished_at"), tz), _text(row.get("trigger"), TRIGGER),
            _result_text(row.get("result_code"), row.get("reason_code")),
            _plugin_link(plugin) if plugin is not None else "-", _floor_cell(row),
            _approval_link(row.get("approval_id"))]


def _more_note(shown: int, has_more: bool, limit: int) -> str:
    if not has_more:
        return ""
    if limit < MAX_LIMIT:
        larger = min(MAX_LIMIT, max(limit + 1, limit * 2))
        return (f'<p>最新 {shown} 件を表示。<a href="/improve?limit={larger}">'
                f"さらに古い run を表示 ({larger} 件まで)</a></p>")
    return f"<p>最新 {shown} 件を表示。これより古い run は afx ctl improve runs で確認してください</p>"


def render_improve_list(data: Mapping[str, object], limit: int = DEFAULT_LIMIT,
                        forms: str = "") -> bytes:
    tz = zone_of(data)
    listing = _pick(data, LIST_FIELDS)
    runs = listing.get("runs")
    rows = [_list_row(row, tz) for row in runs[:ROW_LIMIT] if type(row) is dict] \
        if type(runs) is list else []
    intro = "<p>終了した改善 mission を新しい順に並べています。run の番号から要約を開けます。</p>"
    note = _more_note(len(rows), listing.get("has_more") is True, limit)
    # 書込みの form を足す余地と、枠・nav・style を引いた残りが table の予算
    budget = (render.MAX_RESPONSE_BYTES - len(render._document("Improve", intro + note, tz))
              - render._bytes(forms) - 1024)
    table = _table(LIST_HEADERS, rows, budget) if rows else "<p>終了した改善 mission はありません</p>"
    return render._page("Improve", intro + table + note + forms, tz)


# ---------------------------------------------------------------- 詳細

def _overview(row: dict, snapshot: dict, approval: dict) -> str:
    approval_cell = _approval_link(approval.get("id"))
    plugin = approval.get("plugin")
    if plugin is not None:
        approval_cell += " " + _plugin_link(plugin)
        if approval.get("kind") is not None:
            approval_cell += " (" + _text(approval.get("kind"), KIND) + ")"
    elapsed = row.get("elapsed_sec")
    pairs = [
        ("run", _text(row.get("id"))),
        ("結果", _result_text(snapshot.get("result_code"), snapshot.get("reason_code"))
         if snapshot else "-"),
        ("起動", _text(snapshot.get("trigger"), TRIGGER) if snapshot else "-"),
        ("backend", _text(row.get("backend"))), ("model", _text(row.get("model"))),
        ("経過 (秒)", _num(elapsed)), ("tool 呼出し", render._count_text(snapshot.get("tool_calls"))),
        ("レポート", _text(row.get("report_state"), REPORT_STATE)),
        ("承認", approval_cell if approval else "-"),
    ]
    return _section("概要", render._rows(pairs))


def _text_section(title: str, items: list[tuple[str, object]]) -> str:
    body = "".join(f"<h3>{name}</h3>{_quote(value)}" for name, value in items)
    return _section(title, body)


def _hypothesis_section(items: dict) -> str:
    hypothesis = _pick(items.get("hypothesis"), HYPOTHESIS_FIELDS)
    if not hypothesis:
        return ""
    body = _quote(hypothesis.get("idea"))
    if type(hypothesis.get("backlog_id")) is int:
        body += f"<p>backlog #{_num(hypothesis.get('backlog_id'))}</p>"
    return _section("仮説", body)


def _explanation_section(items: dict) -> str:
    explanation = _pick(items.get("explanation"), EXPLANATION_FIELDS)
    if not explanation:
        return ""
    parts = [("理由", explanation.get("rationale")),
             ("提出物の要約", explanation.get("artifact_summary"))]
    shown = [(name, value) for name, value in parts if value is not None]
    return _text_section("agent の説明", shown) if shown else ""


def _observation_section(items: dict, snapshot: dict) -> str:
    text = items.get("observation")
    if text is None:
        text = snapshot.get("observation_text")
    if text is None or text == "":
        return ""
    return _section("観察文", _quote(text))


def _reason_section(items: dict, snapshot: dict) -> str:
    reason = items.get("parent_reason")
    if reason is None:
        reason = snapshot.get("reason_code")
    if reason is None:
        return ""
    return _section("判定理由", f"<p>{_text(reason, REASON)}</p>")


def _tried_section(items: dict) -> str:
    tried = _pick(items.get("tried"), TRIED_FIELDS)
    if not tried:
        return ""
    names = tried.get("indicators")
    indicators = ""
    if type(names) is list and names:
        shown = [_plugin_link(name) for name in names[:20]]
        indicators = " / ".join(shown)
    pairs = [("plugin", _plugin_link(tried.get("plugin"))),
             ("kind", _text(tried.get("kind"), KIND)),
             ("由来", _text(tried.get("origin"), ORIGIN)),
             ("使った indicator", indicators or "-")]
    return _section("試したもの", render._rows(pairs))


def _thresholds(value: object) -> str:
    limits = _pick(value, THRESHOLD_FIELDS)
    if not limits:
        return ""
    pairs = [("PF の下限", _num(limits.get("min_pf"))),
             ("平均 R を正にする", _bool(limits.get("require_positive_avg_r"))),
             ("holdout の評価を必須にする", _bool(limits.get("require_holdout_evaluable"))),
             ("DD の上限", _num(limits.get("max_drawdown"))),
             ("取引頻度の上限", _num(limits.get("max_trades_per_eval_bar")))]
    return "<h3>床 (閾値)</h3>" + render._rows(pairs)


PAIR_HEADERS = ("pair", "取引数", "PF", "平均 R", "DD", "取引頻度", "評価可", "割った条件")


def _pair_row(pair: dict) -> list[str]:
    return [_text(pair.get("pair")),
            render._count_text(pair.get("trades")), _num(pair.get("pf")), _num(pair.get("avg_r")),
            _num(pair.get("max_drawdown")), _num(pair.get("trades_per_eval_bar")),
            _bool(pair.get("evaluable")), _conditions(pair.get("failed"))]


def _floor_section(items: dict, report_floor: dict) -> str:
    floor = _pick(items.get("floor"), FLOOR_FIELDS)
    if floor:
        body = _thresholds(floor.get("thresholds"))
        stage = floor.get("failed_stage")
        if stage is not None or floor.get("failed_conditions"):
            body += ("<p class=\"warn\">床を割った段: " + _text(stage, STAGE) + " / 条件: "
                     + _conditions(floor.get("failed_conditions")) + " / pair 数: "
                     + render._count_text(floor.get("failed_pair_count")) + "</p>")
        stages, hidden_stages = _list_of(floor.get("stages"), STAGE_FIELDS)
        each = SECTION_BUDGET["floor"] // max(1, len(stages))
        for entry in stages:
            pairs, hidden = _list_of(entry.get("pairs"), PAIR_FIELDS)
            body += (f"<h3>{_text(entry.get('stage'), STAGE)} ({_text(entry.get('state'), STAGE_STATE)})</h3>"
                     + (_table(PAIR_HEADERS, [_pair_row(p) for p in pairs], each, hidden)
                        if pairs else "<p>pair の数値はありません</p>"))
        return _section("床との差", body) if body else ""
    stages, _hidden = _list_of(report_floor.get("stages"), frozenset({"stage", "pairs"}))
    rows = []
    for entry in stages:
        pairs, _ = _list_of(entry.get("pairs"), REPORT_PAIR_FIELDS)
        rows += [[_text(entry.get("stage"), STAGE), _text(p.get("pair")),
                  render._count_text(p.get("trades")), _num(p.get("pf")),
                  _num(p.get("avg_r")), _bool(p.get("evaluable")),
                  _report_conditions(p.get("conditions"))] for p in pairs]
    if not rows:
        return ""
    return _section("床との差 (レポートから)", _table(
        ("段", "pair", "取引数", "PF", "平均 R", "評価可", "条件"), rows, SECTION_BUDGET["floor"]))


def _report_conditions(value: object) -> str:
    if type(value) is not list or not value:
        return "-"
    return " / ".join(_text(word) for word in value[:CELL_ITEMS])


def _side(value: object) -> str:
    side = _pick(value, SIDE_FIELDS)
    if not side:
        return "-"
    return (f"PF {_num(side.get('pf'))} / 平均 R {_num(side.get('avg_r'))} / "
            f"{render._count_text(side.get('trades'))} 件 / 評価可 {_bool(side.get('evaluable'))}")


def _comparison_section(items: dict) -> str:
    comparison = _pick(items.get("comparison"), COMPARISON_FIELDS)
    if not comparison:
        return ""
    metrics = comparison.get("metrics")
    names = " / ".join(html.escape(label(METRIC, word), quote=True)
                       for word in metrics[:4]) if type(metrics) is list and metrics else "-"
    body = render._rows([("モード", _text(comparison.get("mode"), COMPARISON_MODE)),
                         ("判定", _text(comparison.get("verdict"), COMPARISON_VERDICT)),
                         ("理由", _text(comparison.get("reason"), COMPARISON_REASON)),
                         ("段", _text(comparison.get("stage"), STAGE)),
                         ("比べた指標", names)])
    pairs, hidden = _list_of(comparison.get("pairs"), COMPARISON_PAIR_FIELDS)
    if pairs:
        rows = [[_text(p.get("pair")), _text(p.get("verdict"), COMPARISON_VERDICT),
                 _side(p.get("candidate")), _side(p.get("deployed"))] for p in pairs]
        body += _table(("pair", "判定", "候補", "配備中の版"), rows,
                       SECTION_BUDGET["comparison"], hidden)
    return _section("配備中の版との比較 (holdout)", body)


def _handoff_section(items: dict) -> str:
    handoff = _pick(items.get("handoff"), HANDOFF_FIELDS)
    if not handoff:
        return ""
    outcomes = handoff.get("outcomes")
    rows = []
    if type(outcomes) is dict:
        for word, count in sorted(outcomes.items())[:20]:
            if type(word) is str and type(count) is int:
                rows.append([html.escape(label(REASON, word), quote=True), _num(count)])
    body = render._rows([("系統", render._cut(handoff.get("family"), FAMILY_CHARS) or "-"),
                         ("試行回数", render._count_text(handoff.get("attempts")))])
    if rows:
        body += _table(("結果", "回数"), rows, SECTION_BUDGET["handoff"])
    return _section("次回への申し送り (系統の試行結果)", body)


def render_improve_detail(data: Mapping[str, object]) -> bytes:
    tz = zone_of(data)
    row = _pick(data, DETAIL_FIELDS)
    snapshot = _pick(row.get("snapshot"), SNAPSHOT_FIELDS)
    approval = _pick(row.get("approval"), APPROVAL_FIELDS)
    summary = _pick(row.get("summary"), SUMMARY_FIELDS)
    state = summary.get("state")
    items = _pick(summary.get("items"), ITEM_FIELDS) if state == "ok" else {}
    run_id = row.get("id")
    shown = run_id if type(run_id) is int and run_id >= 1 else "-"
    nav = '<p><a href="/improve">Improve (一覧)</a> / <a href="/plugins">Plugins</a></p>'
    if not snapshot:
        parts = [nav, _section("概要", f"<p>{NO_TERMINAL_TEXT}</p>")]
        return render._page(f"Improve #{shown}", "".join(parts), tz)
    if not summary:
        notice = _section("要約", f"<p>{NO_SUMMARY_TEXT}</p>")
    elif state != "ok":
        notice = _section("要約", f"<p>{html.escape(SUMMARY_NOTES.get(state, '要約を表示できません'))}</p>")
    else:
        notice = ""
    parts = [nav, _overview(row, snapshot, approval), notice,
             _hypothesis_section(items), _explanation_section(items),
             _observation_section(items, snapshot), _reason_section(items, snapshot),
             _tried_section(items), _floor_section(items, _pick(row.get("floor"), REPORT_FLOOR_FIELDS)),
             _comparison_section(items), _handoff_section(items)]
    return render._page(f"Improve #{shown}", "".join(parts), tz)
