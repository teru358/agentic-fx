"""承認依頼の「親が受理・記録した内容」と「agent の自己申告」の分離。

保存側 (`build_facts_payload`) は approval を作る transaction の中で、親が
受理・計測・状態遷移として記録した構造化値だけを `parent_facts` に組む。agent の
自由文は `agent_claims` にだけ置く。表示側 (`render_facts_lines` ほか) は保存形式が
壊れていても例外にせず、読めない欄だけを不正形式表示にする。表示する全文字列は
`display_text` 1 本を通る。このモジュールは標準ライブラリだけに依存する。
"""
from __future__ import annotations

import math
import re
import sqlite3
import unicodedata

FACTS_VERSION = 1

MAX_PRIOR_RUNS = 10
MAX_TRIALS = 20
CLAIM_STORE_LIMIT = 2000
CLAIM_DISPLAY_LIMIT = 600
FIELD_DISPLAY_LIMIT = 200
TRUNCATION_SUFFIX = "…(省略)"

INVALID_FORMAT_TEXT = "(保存形式が不正で表示できません)"
PAYLOAD_INVALID_LINE = f"payload: {INVALID_FORMAT_TEXT}"
PARENT_HEADING = "--- 親が受理・記録した内容 ---"
PARENT_INVALID_LINE = f"{PARENT_HEADING} {INVALID_FORMAT_TEXT}"
LEGACY_LINE = (f"{PARENT_HEADING} 旧形式の承認依頼です。"
               "親の事実表は保存されていません。以下の自己申告は未検証です。")
COLLECTION_FAILED_LINE = (
    f"{PARENT_HEADING} 親の事実表の収集に失敗した承認依頼です "
    "(理由は技術ログ)。親の事実表は保存されていません。"
    "以下の自己申告は未検証です。")
UNKNOWN_VERSION_LINE = (
    f"{PARENT_HEADING} 未知の版の承認依頼です。"
    "この版の親の事実表は読めません。以下の自己申告は未検証です。")
FACTS_ERROR_COLLECTION_FAILED = "collection_failed"
CLAIMS_HEADING = "--- agent の自己申告 (未検証。親は内容の真偽を確認していない) ---"
CLAIMS_INVALID_LINE = f"  自己申告: {INVALID_FORMAT_TEXT}"
PARENT_NOTE = ("注記: backtest の回数と同じ内容の再実行は agent が依頼したもので、"
               "試行の独立性を示しません。どの課題を選ぶかは agent が決めています。")

_PARENT_KEYS = frozenset({"backlog", "prior_runs", "trials", "trials_omitted",
                          "analysis_call_count"})
_BACKLOG_KEYS = frozenset({"id", "attempts", "status"})
_TRIAL_KEYS = frozenset({"n", "content_hash8", "same_hash_as_submitted",
                         "trades", "pf", "avg_r", "max_drawdown"})
_PRIOR_RUN_KEYS = frozenset({"run_id", "result", "approval_id"})
_BACKLOG_STATUSES = frozenset({"open", "observation", "note", "selected",
                               "done", "rejected"})
# improvement_runs.result が NULL の run を、mission の status と
# report_state で分類した値。None は区別に使える情報が無い run
# (mission が無い等) を表し、観測とも失敗とも断定しない。
_RESULTS = (None, "approval", "report", "observation", "failed",
            "interrupted", "report_failed")
_HASH8_RE = re.compile(r"[0-9a-f]{8}")
_AGENT_ORIGINS = frozenset({"agent", "research"})
_NEWLINE_TABLE = {ord("\r"): " ", ord("\n"): " ", ord("\t"): " "}
_REMOVED_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


class FactsError(ValueError):
    """`parent_facts` が仕様の形を満たさない。"""


# ---------------------------------------------------------------- 無害化

def display_text(value: object, limit: int = FIELD_DISPLAY_LIMIT) -> str:
    """表示する全文字列の唯一の無害化。非文字列は `-` (str() で表示しない)。
    CR/LF/TAB は空白へ、Cc・Cf・Cs は除去し、連続空白を畳んで 1 行にする。
    空値は `-`。`limit` 文字を超えたら切り捨てて固定接尾辞を付ける。"""
    if not isinstance(value, str):
        return "-"
    text = value.translate(_NEWLINE_TABLE)
    text = "".join(ch for ch in text
                   if unicodedata.category(ch) not in _REMOVED_CATEGORIES)
    text = " ".join(text.split())
    if not text:
        return "-"
    # 保存時に切って接尾辞が付いた値は、接尾辞を除いた本体で長さを測り、
    # 接尾辞が重ならないようにする。
    body = text[:-len(TRUNCATION_SUFFIX)] if text.endswith(
        TRUNCATION_SUFFIX) else text
    if len(body) <= limit:
        return text
    return body[:limit] + TRUNCATION_SUFFIX


def _store_text(value: object) -> str | None:
    """保存時の自由文: 文字列だけを残し、2,000 文字で切る。"""
    if not isinstance(value, str):
        return None
    if len(value) > CLAIM_STORE_LIMIT:
        return value[:CLAIM_STORE_LIMIT] + TRUNCATION_SUFFIX
    return value


# ---------------------------------------------------------------- 検証

def _is_int(value: object, *, minimum: int) -> bool:
    return type(value) is int and value >= minimum


def _is_number(value: object) -> bool:
    if type(value) is int:
        return True
    return type(value) is float and math.isfinite(value)


def _is_number_or_none(value: object) -> bool:
    return value is None or _is_number(value)


def validate_parent_facts(facts: object) -> None:
    """キー集合・型・列挙値を厳密に検査する。違反は `FactsError`。"""
    if not isinstance(facts, dict) or set(facts) != _PARENT_KEYS:
        raise FactsError("parent_facts keys")
    backlog = facts["backlog"]
    if backlog is not None:
        if not isinstance(backlog, dict) or set(backlog) != _BACKLOG_KEYS:
            raise FactsError("backlog keys")
        if not _is_int(backlog["id"], minimum=1):
            raise FactsError("backlog.id")
        if not _is_int(backlog["attempts"], minimum=0):
            raise FactsError("backlog.attempts")
        if backlog["status"] not in _BACKLOG_STATUSES:
            raise FactsError("backlog.status")
    prior_runs = facts["prior_runs"]
    if not isinstance(prior_runs, list) or len(prior_runs) > MAX_PRIOR_RUNS:
        raise FactsError("prior_runs")
    for run in prior_runs:
        if not isinstance(run, dict) or set(run) != _PRIOR_RUN_KEYS:
            raise FactsError("prior_runs element keys")
        if not _is_int(run["run_id"], minimum=1):
            raise FactsError("prior_runs.run_id")
        result, approval_id = run["result"], run["approval_id"]
        if result not in _RESULTS:
            raise FactsError("prior_runs.result")
        if result == "approval":
            if not _is_int(approval_id, minimum=1):
                raise FactsError("prior_runs.approval_id")
        elif approval_id is not None:
            raise FactsError("prior_runs.approval_id")
    trials = facts["trials"]
    if not isinstance(trials, list) or len(trials) > MAX_TRIALS:
        raise FactsError("trials")
    for trial in trials:
        if not isinstance(trial, dict) or set(trial) != _TRIAL_KEYS:
            raise FactsError("trial keys")
        if not _is_int(trial["n"], minimum=1):
            raise FactsError("trial.n")
        hash8 = trial["content_hash8"]
        if not (isinstance(hash8, str) and _HASH8_RE.fullmatch(hash8)):
            raise FactsError("trial.content_hash8")
        if type(trial["same_hash_as_submitted"]) is not bool:
            raise FactsError("trial.same_hash_as_submitted")
        if not _is_int(trial["trades"], minimum=0):
            raise FactsError("trial.trades")
        for key in ("pf", "avg_r", "max_drawdown"):
            if not _is_number_or_none(trial[key]):
                raise FactsError(f"trial.{key}")
    if not _is_int(facts["trials_omitted"], minimum=0):
        raise FactsError("trials_omitted")
    if not _is_int(facts["analysis_call_count"], minimum=0):
        raise FactsError("analysis_call_count")


# ---------------------------------------------------------------- 保存側

def collection_failed_payload() -> dict:
    """親の事実表の収集に失敗したことを示す固定の印。旧形式 (印なし) とも
    成功形とも区別して表示される。"""
    return {"facts_version": FACTS_VERSION, "parent_facts": None,
            "facts_error": FACTS_ERROR_COLLECTION_FAILED}


def _trial_from_entry(n: int, entry: dict, submitted_hash: object) -> dict:
    summary = entry["result_summary"]
    metrics = summary["metrics"]
    content_hash = summary["content_hash"]
    return {
        "n": n,
        "content_hash8": content_hash[:8],
        "same_hash_as_submitted": content_hash == submitted_hash,
        "trades": metrics["trades"],
        "pf": metrics.get("pf"),
        "avg_r": metrics.get("avg_r"),
        "max_drawdown": metrics.get("max_drawdown"),
    }


def _run_result(result: object, mission_status: object,
                report_state: object) -> object:
    """`improvement_runs.result` が NULL の run を mission の status と
    report_state で分ける。区別に使える行が無ければ None のまま。"""
    if result is not None:
        return result
    if mission_status == "failed":
        return "failed"
    if mission_status == "interrupted":
        return "interrupted"
    if report_state == "failed":
        return "report_failed"
    if mission_status == "completed" and report_state == "none":
        return "observation"
    return None


def build_facts_payload(conn: sqlite3.Connection, *, run_id: int,
                        backlog_id: int | None, accepted_entries,
                        submitted_content_hash: object,
                        selection_rationale: object, summary: object) -> dict:
    """approval を作る transaction の中で呼ぶ。`facts_version` /
    `parent_facts` / `agent_claims` を返す。形が壊れていれば `FactsError`。

    `accepted_entries` は親が受理した台帳 entry (error の無いもの) の全件。"""
    backlog = None
    backlog_row = None
    if backlog_id is not None:
        backlog_row = conn.execute(
            "SELECT id, attempts, status, source, idea "
            "FROM improvement_backlog WHERE id=?", (backlog_id,)).fetchone()
        if backlog_row is not None:
            backlog = {"id": backlog_row["id"],
                       "attempts": backlog_row["attempts"],
                       "status": backlog_row["status"]}
    prior_runs = []
    if backlog_id is not None:
        for row in conn.execute(
                "SELECT r.id, r.result, r.approval_id, r.report_state, "
                "m.status AS mission_status "
                "FROM improvement_runs r "
                "LEFT JOIN missions m ON m.id=r.mission_id "
                "WHERE r.backlog_id=? AND r.id<>? "
                "AND r.finished_at IS NOT NULL "
                "ORDER BY r.finished_at DESC, r.id DESC LIMIT ?",
                (backlog_id, run_id, MAX_PRIOR_RUNS)):
            prior_runs.append({
                "run_id": row["id"],
                "result": _run_result(row["result"], row["mission_status"],
                                      row["report_state"]),
                "approval_id": row["approval_id"]})
    entries = list(accepted_entries)
    try:
        backtests = [e for e in entries if e["kind"] == "run_backtest"]
        analysis_call_count = sum(
            1 for e in entries if e["kind"] == "analyze_corr")
        all_trials = [_trial_from_entry(i, e, submitted_content_hash)
                      for i, e in enumerate(backtests, start=1)]
    except (KeyError, TypeError, AttributeError) as exc:
        raise FactsError(f"ledger entry shape: {exc!r}") from exc
    # 提出と同一の行は件数上限に関わらず残し、残りの枠を受理順の先頭で埋める。
    same = [t for t in all_trials if t["same_hash_as_submitted"]][:MAX_TRIALS]
    others = [t for t in all_trials if not t["same_hash_as_submitted"]]
    kept = same + others[:MAX_TRIALS - len(same)]
    trials = sorted(kept, key=lambda t: t["n"])
    parent_facts = {
        "backlog": backlog,
        "prior_runs": prior_runs,
        "trials": trials,
        "trials_omitted": len(all_trials) - len(trials),
        "analysis_call_count": analysis_call_count,
    }
    validate_parent_facts(parent_facts)
    claims: dict = {}
    for key, value in (("selection_rationale", selection_rationale),
                       ("summary", summary)):
        stored = _store_text(value)
        if stored is not None:
            claims[key] = stored
    if (backlog_row is not None and backlog_row["status"] == "selected"
            and backlog_row["source"] in _AGENT_ORIGINS):
        stored = _store_text(backlog_row["idea"])
        if stored is not None:
            claims["selected_backlog_idea"] = stored
    return {"facts_version": FACTS_VERSION, "parent_facts": parent_facts,
            "agent_claims": claims}


# ---------------------------------------------------------------- 表示側

_RESULT_TEXT = {
    None: "結果なし (観測または失敗。区別できる記録がありません)",
    "observation": "観測のみ (承認依頼なし)",
    "report": "報告として完了 (承認依頼なし)",
    "failed": "失敗 (mission status=failed)",
    "interrupted": "失敗 (mission status=interrupted)",
    "report_failed": "報告の作成に失敗 (承認依頼なし)",
}


def num_text(value: object) -> str:
    """数値セルの表示。None は `-`。文字列にして `display_text` の長さ上限を
    通す (検証済みの型でも桁数が際限なく長い値を表示しない)。"""
    return "-" if value is None else display_text(f"{value}")


_num = num_text


def _parent_lines(facts: dict) -> list[str]:
    lines = [PARENT_HEADING]
    backlog = facts["backlog"]
    if backlog is None:
        lines.append("backlog: なし")
    else:
        lines.append(
            f"backlog #{num_text(backlog['id'])} (今回を含め attempts="
            f"{num_text(backlog['attempts'])}) status="
            f"{display_text(backlog['status'])}")
    prior_runs = facts["prior_runs"]
    if not prior_runs:
        lines.append("過去の完了 run: なし")
    else:
        lines.append("過去の完了 run:")
        for run in prior_runs:
            if run["result"] == "approval":
                text = f"承認依頼 #{num_text(run['approval_id'])}"
            else:
                text = _RESULT_TEXT[run["result"]]
            lines.append(f"  run #{num_text(run['run_id'])}: {text}")
    trials = facts["trials"]
    total = len(trials) + facts["trials_omitted"]
    lines.append(f"mission 内の backtest {num_text(total)} 件 "
                 f"(in_sample の受理分) / "
                 f"分析 {num_text(facts['analysis_call_count'])} 件:")
    for trial in trials:
        same = "提出と同一" if trial["same_hash_as_submitted"] else "提出と別"
        lines.append(
            f"  {num_text(trial['n'])}. "
            f"hash={display_text(trial['content_hash8'])} "
            f"({same}) trades={num_text(trial['trades'])} "
            f"pf={_num(trial['pf'])} "
            f"avg_r={_num(trial['avg_r'])} mdd={_num(trial['max_drawdown'])}")
    if facts["trials_omitted"]:
        lines.append(f"  (ほか {num_text(facts['trials_omitted'])} 件は省略)")
    lines.append(PARENT_NOTE)
    return lines


_CLAIM_LABELS = (
    ("selection_rationale", "自己申告: selection_rationale: "),
    ("summary", "自己申告: summary: "),
    ("selected_backlog_idea", "引用: 選んだ課題の文面 (agent 起票): "),
)


def _claims_lines(claims: dict) -> list[str]:
    body = []
    for key, prefix in _CLAIM_LABELS:
        if key not in claims:
            continue
        value = claims[key]
        text = (display_text(value, CLAIM_DISPLAY_LIMIT)
                if isinstance(value, str) else INVALID_FORMAT_TEXT)
        body.append(f"  {prefix}{text}")
    return [CLAIMS_HEADING] + body if body else []


def render_facts_lines(payload: dict) -> list[str]:
    """親欄と自己申告欄の行。`payload` は dict であること。例外にしない。"""
    version = payload.get("facts_version")
    if type(version) is int and version == FACTS_VERSION:
        facts = payload.get("parent_facts")
        if (facts is None and payload.get("facts_error")
                == FACTS_ERROR_COLLECTION_FAILED):
            claims = {key: payload[key]
                      for key in ("selection_rationale", "summary")
                      if key in payload}
            return [COLLECTION_FAILED_LINE] + _claims_lines(claims)
        try:
            validate_parent_facts(facts)
            lines = _parent_lines(facts)
        except Exception:  # noqa: BLE001 — 表示は fail-soft
            lines = [PARENT_INVALID_LINE]
        claims = payload.get("agent_claims")
        if not isinstance(claims, dict):
            return lines + [CLAIMS_HEADING, CLAIMS_INVALID_LINE]
    else:
        lines = [UNKNOWN_VERSION_LINE
                 if type(version) is int else LEGACY_LINE]
        claims = {key: payload[key]
                  for key in ("selection_rationale", "summary")
                  if key in payload}
    return lines + _claims_lines(claims)
