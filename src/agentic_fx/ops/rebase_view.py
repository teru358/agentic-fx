"""`afx ctl killswitch rebase` の表示用の固定文と整形。

値は server の preview DTO をそのまま写す。ここでは金額・率・回数を計算しない。
DTO は信用しない入力として扱い、数値は有限な数だけ、文字列は無害化して出す。"""
from __future__ import annotations

import math
from typing import Callable

from agentic_fx.loops.approval_facts import display_text

CUMULATIVE_WARNING = ("警告: 累積切下げ率（各回率の合計）が現在の DD 閾値の3倍を超えます。\n"
                      "過去の損失を受け入れ、追加の損失余地を戻します。")

SUCCESS_LATCHED = "HWM を付け直した。新規停止は継続中 (ラッチは解除していない)。"
SUCCESS_CLEAR = ("HWM を付け直した。現在の DD 条件は解消した。"
                 "日次損失・総リスク等の gate の通過は約束しない。")
SUCCESS_UNSTATED = "HWM を付け直した。ラッチの状態は `afx ctl status` で確認してください。"

CHANGED = "表示した後に値が変わったため付け直さなかった。もう一度確認してください"

_EXPOSURE = (
    "建玉または未決の注文があるため付け直せません (flat のときだけ可能です)。\n"
    "  APPROVAL_PENDING: `reject` で却下する\n"
    "  PENDING_FILL: 期限切れを待つ\n"
    "  *_UNKNOWN: 定期の解決処理 (`_resolve_unknowns`) を待つ\n"
    "  OPEN・保護途中・その他: SL / TP または判断 loop の終端化を待つ。"
    "未解決なら状態別の運用手順を確認する")

# preview の reason (固定 enum) の日本語。ここに無い語は「不明」として出す。
REASON_TEXT = {
    "rebase_unavailable": "この daemon では HWM の付け直しを使えません (依存が未配線です)",
    "broker_not_supported": "PaperBroker 以外では HWM を付け直せません",
    "state_unavailable": ("kill switch の状態を確定できません。"
                          "`afx ctl --as approver killswitch reconcile` で確認してください"),
    "generation_mismatch": ("kill switch のラッチが更新されたので status を確認してから"
                            "もう一度実行してください"),
    "generation_unavailable": ("世代 0 は付け直せません (kill switch が一度もラッチして"
                               "いない状態です)。初回の DD 拒否でラッチが起きた後に実行してください"),
    "market_closed": ("閉場中は付け直せません。市場が開き、新しい MTM が記録されてから"
                      "再実行してください"),
    "account_snapshot_unavailable": ("新鮮な口座 snapshot がありません。"
                                     "新しい MTM が記録されてから再実行してください"),
    "account_snapshot_changed": ("broker の再評価と snapshot が一致しません。"
                                 "新しい MTM が記録されてから再実行してください"),
    "exposure_present": _EXPOSURE,
    "rebase_not_needed": "現在の DD は閾値未満です。付け直しは要りません",
    "accounting_inconsistent": ("口座の記録に不整合があります (入出金の記録または日次基準)。"
                                "状態と MTM の回復を確認してください"),
    "preview_changed": CHANGED,
}


def reason_text(reason: object) -> str:
    if isinstance(reason, str) and reason in REASON_TEXT:
        return REASON_TEXT[reason]
    return f"付け直せません (不明な理由: {display_text(reason, 64)})"


def amount(value: object) -> str:
    if type(value) in (int, float) and math.isfinite(value):
        return f"{value:,.2f}"
    return "-"


def pct(value: object) -> str:
    if type(value) in (int, float) and math.isfinite(value):
        return f"{value:.2f}%"
    return "-"


def count(value: object) -> str:
    return str(value) if type(value) is int else "-"


def confirm_sentence(data: dict, generation: int) -> str:
    """確認の直前に出す 1 文。今回と累積の幅・率、今回後の回数を DTO から埋める。"""
    return (f"今回 幅 {amount(data.get('cut'))} (率 {pct(data.get('cut_pct'))}) の損失を受け入れ、"
            f"累積は 幅 {amount(data.get('cumulative_cut_after'))} "
            f"(率の合計 {pct(data.get('cumulative_cut_pct_after'))})、"
            f"{count(data.get('count_after'))} 回目の付け直しになります。"
            f"続けるには `rebase {generation}` と入力してください: ")


def preview_lines(data: dict, generation: int, when: Callable[[object], str]) -> list[str]:
    latched = data.get("latched")
    flat = data.get("flat")
    state = "LATCHED" if latched is True else "解除済み" if latched is False else "-"
    lines = [
        f"kill switch の HWM を付け直します (gen={generation})",
        f"ラッチ: {state} 発動時刻={when(data.get('latched_at'))}",
        f"source snapshot: id={count(data.get('source_snapshot_id'))} "
        f"時刻={when(data.get('source_snapshot_ts'))} "
        f"source={display_text(data.get('source_snapshot_source'), 64)}",
        f"現在 HWM={amount(data.get('hwm'))} balance={amount(data.get('balance'))} "
        f"equity={amount(data.get('equity'))}",
        f"付け直し後 HWM={amount(data.get('new_hwm'))}",
        f"現在 DD={pct(data.get('drawdown_pct'))} 閾値={pct(data.get('threshold_pct'))}",
        f"今回の切下げ: 幅 {amount(data.get('cut'))} 率 {pct(data.get('cut_pct'))}",
        f"過去の累積: 幅 {amount(data.get('past_cut'))} 率の合計 {pct(data.get('past_cut_pct'))} "
        f"回数 {count(data.get('past_count'))}",
        f"今回適用後: 累積幅 {amount(data.get('cumulative_cut_after'))} "
        f"累積率の合計 {pct(data.get('cumulative_cut_pct_after'))} "
        f"回数 {count(data.get('count_after'))}",
        f"戻る追加の損失余地: {amount(data.get('additional_headroom'))}",
        f"建玉・未決の注文: {'なし (flat)' if flat is True else '確認できません'}",
        "累積率は各回の直前 HWM に対する切下げ率の単純和で、当初資金に対する実損率では"
        "ありません。",
        "rebase ではラッチを解除しません (ラッチ中は別に `killswitch reset <世代>` が要ります)。"
        "日次損失・総リスク等の gate は変わりません。",
        "閉場中は付け直せません。",
    ]
    if data.get("cumulative_warning") is True:
        lines.append(CUMULATIVE_WARNING)
        lines.append(f"比較対象: 累積率の合計 {pct(data.get('cumulative_cut_pct_after'))} > "
                     f"{pct(data.get('warning_threshold_pct'))} (現在の DD 閾値の3倍)。"
                     "閾値を変更すると警告の判定も変わります。")
    return lines
