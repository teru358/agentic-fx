"""状態の印とカード。値の分類 (割当表) と、印・カードの描画を分けて持つ。

分類は daemon の応答から取り出した生の値と厳密な型だけで行う。表に無い値・欠損・None・型違いは
必ず「? 不明」(色なし) で、正常には倒さない。翻訳や代替表示は分類の後に行い、置き換えた後の値は
分類に使わない。この表示は判断には使わない (決定論のコアは daemon 側)。
"""
from __future__ import annotations

import html
from collections.abc import Mapping, Sequence

OK, WARN, DANGER, NEUTRAL, UNKNOWN = "ok", "warn", "danger", "neutral", "unknown"
_SYMBOL = {OK: "○", WARN: "△", DANGER: "✕", NEUTRAL: "–", UNKNOWN: "?"}
_CLASS = {OK: "st ok", WARN: "st warn", DANGER: "st danger", NEUTRAL: "st", UNKNOWN: "st"}
UNKNOWN_WORD = "不明"

Verdict = tuple[str, str]  # (level, 語)


def mark(level: str, text: str) -> str:
    """level ごとの記号 + 語の印。level が不正・語が空なら不明の印にする。

    記号は文字として出す (CSS に頼らない)。不明の印は渡された語 (「不明 (<語>)」など) を残す。
    """
    if level not in _SYMBOL or not text:
        level, text = UNKNOWN, UNKNOWN_WORD
    return f'<span class="{_CLASS[level]}">{_SYMBOL[level]} {html.escape(text, quote=True)}</span>'


def render_verdict(verdict: Verdict) -> str:
    return mark(*verdict)


def cards(items: Sequence[tuple[str, str]]) -> str:
    """見出しと値 1 つのカードの列。値が空の項目は出さず、全部空なら列ごと出さない。

    値は escape 済みの HTML を渡す。見出しは固定の語。
    """
    shown = [f"<div><strong>{html.escape(label, quote=True)}</strong>{value}</div>"
             for label, value in items if value]
    return '<div class="kpi">' + "".join(shown) + "</div>" if shown else ""


_UNKNOWN: Verdict = (UNKNOWN, UNKNOWN_WORD)

# --- 語 → level の割当表。語の集合は語彙の写し (render・plugin_text など) と test が突き合わせる ---

# data (daemon の datafeed outage の state。定義は agentic_fx.datafeed.outage.DATA_STATES の写し)
DATA_STATES = {"ready": OK, "degraded": WARN, "restricted": WARN}
# data の案内の phase (定義は agentic_fx.datafeed.guidance_text.PHASES の写し)。停止・制限・待ちは
# DATA_STATES と同じく warn、読めないものは不明 (正常には倒さない)
DATA_PHASES = {"ready": OK, "resume_requested": WARN, "resume_rejected": WARN,
               "awaiting_resume": WARN, "stopped": WARN, "restricted": WARN,
               "recovering": WARN, "unknown": UNKNOWN}
DATA_PHASE_TEXT = {"ready": "停止なし", "resume_requested": "再開要求の判定待ち",
                   "resume_rejected": "再開要求が拒否", "awaiting_resume": "再開操作待ち",
                   "stopped": "停止中", "restricted": "制限中", "recovering": "自動解除の確認中",
                   "unknown": UNKNOWN_WORD}
# bot の gateway health・calendar・worker・容量の語彙は、この表のキーが唯一の定義 (render が写さず導く)
BOT_HEALTH = {"healthy": OK, "unhealthy": DANGER, "stalled": DANGER, "stale": WARN, "unknown": WARN,
              "not_configured": NEUTRAL, "permanent-failure": DANGER}
CALENDAR = {"unknown": NEUTRAL, "fresh": OK, "stale": WARN}
WORKER = {"alive": OK, "stopped": DANGER, "start_failed": DANGER, "not_started": NEUTRAL,
          "unknown": NEUTRAL}
CAPACITY = {"ok": OK, "warn": WARN, "full": DANGER}


# plugin (一覧の配備列・詳細のカード)。語は plugin_text の写しと突き合わせる
DEPLOY = {"symlink": OK, "plain": OK, "unreadable": DANGER, "absent": NEUTRAL}
ADMISSION = {"loaded": OK, "excluded": WARN, "unapproved": WARN, "none": NEUTRAL,
             "unknown": NEUTRAL}
# improve (一覧の結果列・詳細のカード)
IMPROVE_RESULT = {"submitted": OK, "report": OK, "gate_failed": WARN, "failed": DANGER,
                  "observation": NEUTRAL}
# 承認詳細の status
APPROVAL_STATUS = {"approved": OK, "pending": WARN, "rejected": NEUTRAL, "expired": NEUTRAL,
                   "invalidated": NEUTRAL}


# performance (今の資金保護の状態)。語は daemon の performance 応答の写し
FILL_HOLD = {"kill_switch": DANGER, "unknown": UNKNOWN, "stale": WARN, "drawdown": DANGER,
             "none": OK}
FRESHNESS = {"fresh": OK, "stale": WARN, "unknown": UNKNOWN}

# --- 表示の語。daemon の生の語を使う人の言葉に替える (分類はこの語に依らない)。Status と
# Performance はどちらもここから出す ---

# 鮮度・状態の語 (経済指標の calendar・bot の gateway・worker・最新記録の鮮度)
WORD_TEXT = {"fresh": "新しい", "stale": "古い", "unknown": UNKNOWN_WORD}
# 新規の約定を止めている理由。停止の語は弱めない
FILL_HOLD_TEXT = {"none": "止めていない", "kill_switch": "kill switch で停止",
                  "stale": "記録が古く停止", "drawdown": "DD 到達で停止", "unknown": UNKNOWN_WORD}


def level_of(table: Mapping[str, str], raw: object) -> str:
    """表にある語の level。表に無い値・型違いは不明 (正常に倒さない)。"""
    return table[raw] if type(raw) is str and raw in table else UNKNOWN


def classify_word(table: Mapping[str, str], raw: object,
                  shown: Mapping[str, str] | None = None) -> Verdict:
    """表にある語だけを分類する。表に無い値・型違いは不明。

    語は既定ではそのまま出し、`shown` があれば分類の後に表示だけをその語に替える
    (替えた語は分類に使わない)。"""
    if type(raw) is str and raw in table:
        return table[raw], (shown.get(raw, raw) if shown else raw)
    return _UNKNOWN


def classify_kill_switch(raw: object) -> Verdict:
    if type(raw) is not dict:
        return _UNKNOWN
    latched, reconcile = raw.get("latched"), raw.get("reconcile_required")
    if latched is True:
        return DANGER, "作動中"
    if latched is not False:
        return _UNKNOWN
    if reconcile is True:
        return WARN, "照合待ち"
    if reconcile is False:
        return OK, "解除"
    return _UNKNOWN


def _count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def classify_health(raw: object) -> Verdict:
    if type(raw) is not dict:
        return _UNKNOWN
    latched, failures = raw.get("latched"), _count(raw.get("failures"))
    if latched is True or (failures is not None and failures >= 1):
        return WARN, (f"異常の記録 {failures} 件" if failures else "異常の記録")
    if latched is False and failures == 0:
        return OK, "正常"
    return _UNKNOWN


def classify_autopilot(raw: object) -> Verdict:
    if raw is True:
        return WARN, "ON"
    if raw is False:
        return NEUTRAL, "OFF"
    return _UNKNOWN


def classify_data(raw: object) -> Verdict:
    return classify_word(DATA_STATES, raw.get("state") if type(raw) is dict else None)


def classify_data_phase(raw: object) -> Verdict:
    """案内の phase の印。表に無い語・型違いは不明 (ready にしない)。"""
    return classify_word(DATA_PHASES, raw.get("phase") if type(raw) is dict else None,
                         DATA_PHASE_TEXT)
