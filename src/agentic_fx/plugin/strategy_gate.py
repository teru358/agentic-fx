"""evaluate_strategy_adoption_gate — 戦略採用ゲート (設計書 §4.2-4、
プラン §8.1-40)。`ImproveLoop.commit` 手順4 と Task 11 の
`bless --from _human` (§8.1-41) の両方から import される共有モジュール
(統合裁定 R-i3 — plugin/ 配下の独立ファイルに置く)。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Literal

from agentic_fx.backtest import holdout
from agentic_fx.backtest.dataset import HISTORY_SOURCE_UNCONFIGURED_REASON
from agentic_fx.backtest.metrics import EVALUABLE_MIN_TRADES
from agentic_fx.plugin import strategy_adapter
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import SandboxError

if TYPE_CHECKING:
    from agentic_fx.plugin.resolve import (
        InventoryBuildResult, ResolvedIndicatorSet,
    )

_log = logging.getLogger("agentic_fx.plugin.strategy_gate")

# timeframe 正規化の単一所有者 (codex 段階2/3 是正 1周目): `plugin/
# approval.py` はこの辞書を再実装せず `strategy_gate._eval_timeframe` を
# 直接参照する。switch.py (submit/bless) と improve_loop.py の payload
# 組み立ても同じ関数を経由すること (4 系統の eval_timeframe 写像を 1 箇所
# に集約する)。
_EVAL_TIMEFRAME_OVERRIDE = {"1d": "24h"}


def _eval_timeframe(meta_timeframe: str) -> str:
    return _EVAL_TIMEFRAME_OVERRIDE.get(meta_timeframe, meta_timeframe)


@dataclass(frozen=True)
class StrategyGateVerdict:  # 新規命名 (元 _StrategyGateVerdict — 独立
                            # モジュールへ切り出すため公開名にする)
    evaluable: bool
    observation_reason: str = ""
    baseline_variant: str = "no_strategy"
    baseline_row: dict | None = None
    candidate_metrics: dict | None = None
    # [profitability-floor] T1 Step 1-2 (2026-09-12、設計書 §3 T1-a):
    # `evaluable` は R8 の「標本不足」意味論を流用しない (別フィールド)。
    floor_reason: str = ""
    floor_detail: str = ""
    # [profitability-floor-fix] G2 (2026-09-13): holdout 段の pair→metrics
    # (`_run_holdout` の戻り値をそのまま、`candidate_metrics` の holdout
    # 版)。in_sample 段で enforce 不合格になり holdout を回さなかった
    # ときは `None` のまま (改善ループのレポート本文が holdout 表の
    # 有無を判定する材料 — plan pin 「in_sample で落ちた場合は holdout
    # 表無し」)。`record_fn` の sink (gate_rows) 経由で持ち直さない —
    # sink の `metrics` は永続化用に呼び出し元が別途組み立てる値であり
    # (テストの fake がそうであるように) 判定に使った実際の pf/avg_r と
    # 一致する保証がないため、判定元の dict をそのまま持たせる。
    holdout_metrics: dict | None = None
    # [indicator-consumption-wiring] §2.9(e): (scope, pair, cpu_sec|None)。
    # scope in {"in_sample", "holdout"}。
    cpu_samples: "tuple[tuple[str, str, float | None], ...]" = ()
    # evaluable=False の理由の種別 (判別子)。呼び出し側は observation_reason の
    # 文字列ではなくこの値で分岐する。
    unevaluable_kind: Literal["insufficient_trades",
                              "history_source_unconfigured"] = "insufficient_trades"
    # 配備中の同名版との比較 (`compare_mode` が shadow / enforce で、配備中の
    # 版があり、候補が床を通ったときだけ非 None)。`comparison` は承認
    # payload の `comparison` にそのまま載せる JSON 安全な dict。
    # `not_improved` は比較が `not_improved` だったことを示すだけで、却下
    # するかは呼び出し側が mode で決める (shadow では却下に使わない)。
    comparison: dict | None = None
    not_improved: bool = False
    # 同名更新として不正 (足か pair 集合の変更)。空でなければ候補の
    # backtest は 1 行も回していない。値は固定語。
    contract_violation: str = ""
    # 再生失敗のうち `unavailable` に倒したときの `SandboxError.code`
    # (承認 payload には載せない、運用ログ用)。
    baseline_failure_code: str = ""
    # 配備中の版の再生で得た行 (`variant="baseline"`)。再生が最後まで成功した
    # ときだけ入り、gate は `record_fn` へ流さない。永続化は呼び出し側が
    # 候補の行と同じ終端 tx で行う。
    baseline_rows: tuple[dict, ...] = ()


def _floor_fail_items(
        per_pair: dict, *, settings: "Settings",
        scope: str) -> list[tuple[str, str]]:
    """[profitability-floor-fix] G2 (2026-09-13): `_check_profitability_
    floor` の判定条件だけを取り出した内部 helper — pair ごとの不合格理由
    を `(pair, message)` のリストで返す (`_check_profitability_floor` は
    ここから文字列を組み立てるだけにし、判定条件を 2 箇所に複製しない)。

    順序 ①→②→③ は契約 (codex I9): ① (strict holdout 判定) を ② の
    後に置くと `require_holdout_evaluable=True` でも `trades=0` の pair
    が通ってしまう。
    """
    g = settings.improve.gate
    items: list[tuple[str, str]] = []
    for pair, m in per_pair.items():
        # ① strict holdout 判定は zero-trade shortcut より前 (trades==0
        # でも FAIL する)。
        if scope == "holdout" and g.require_holdout_evaluable \
                and not m.get("evaluable"):
            items.append((pair, "holdout_not_evaluable"))
            continue
        # ② 成績が無い pair は通常モードでは判定から除外する。
        if m["trades"] == 0:
            continue
        # ③ 既定: holdout の標本不足は「悪いとは言わない」(R8)。
        if scope == "holdout" and not m.get("evaluable"):
            continue
        pf = m["pf"]
        if pf is not None and pf < g.min_pf:  # pf is None (gross_loss==0) は PASS
            items.append((pair, f"pf={pf}"))
            continue
        avg_r = m["avg_r"]
        if g.require_positive_avg_r and avg_r is not None and avg_r <= 0.0:
            items.append((pair, f"avg_r={avg_r}"))
    return items


def _check_profitability_floor(
        per_pair: dict, *, settings: "Settings", scope: str) -> tuple[str, str]:
    """[profitability-floor] T1 Step 1-1 (設計書 §3「判定規則 (逐語)」)。

    戻り値 `(label, detail)`。`label` は `""` (合格) か `"unprofitable"`
    (固定文言 — この文字列は `improvement_backlog.last_result` に逐語で
    現れるため変えない)。`detail` は人間向けレポート専用の全数値文字列
    — `label` と混ぜない (遮断 8 の 1 bit 例外を守るため)。

    pair ごとに判定し、**1 pair でも不合格なら候補全体を落とす**
    (pf は pair 間で算術合成できないため合計ではなく pair ごとに見る)。

    判定条件そのものは `_floor_fail_items` に委譲する (二重実装しない)。
    """
    items = _floor_fail_items(per_pair, settings=settings, scope=scope)
    if items:
        detail = ", ".join(f"{pair}: {msg}" for pair, msg in items)
        return "unprofitable", f"{scope}: {detail}"
    return "", ""


SAME_NAME_CONTRACT_CHANGED = "same_name_contract_changed"

_COMPARE_METRIC_KEYS = (
    "trades", "pf", "win_rate", "avg_r", "max_drawdown", "total_pnl",
    "evaluable", "fallback_spread_used")

CompareVerdict = Literal["improved", "not_improved", "unavailable"]


def compare_metrics(baseline: dict, candidate: dict) -> CompareVerdict:
    """1 pair・1 段の比較。両側が `evaluable` でなければ `unavailable`。
    それ以外は pf と avg_r が共に配備中の版を厳密に上回るときだけ
    `improved`。`pf is None` (損失ゼロ) は +inf、双方 None は等しい。
    avg_r が None の側は上回れない (fail closed)。"""
    if not (baseline.get("evaluable") and candidate.get("evaluable")):
        return "unavailable"

    def _pf(v):
        return float("inf") if v is None else v

    if not _pf(candidate.get("pf")) > _pf(baseline.get("pf")):
        return "not_improved"
    b_r, c_r = baseline.get("avg_r"), candidate.get("avg_r")
    if b_r is None or c_r is None or not c_r > b_r:
        return "not_improved"
    return "improved"


# `SandboxError.code` の全数 (sandbox.py の生成箇所と既定値) の扱い。
# 表に無い code は未知として fail closed。
_REPLAY_CODES_UNAVAILABLE = frozenset({
    "plugin_error", "crashed", "protocol_error", "backtest_failed",
    "timeout", "cpu_limit"})
_REPLAY_CODE_RETRYABLE = "sandbox_unavailable"


def classify_replay_failure(
        code: str) -> Literal["retryable", "unavailable", "fail_closed"]:
    if code == _REPLAY_CODE_RETRYABLE:
        return "retryable"
    if code in _REPLAY_CODES_UNAVAILABLE:
        return "unavailable"
    return "fail_closed"


class _ReplayDeadline(Exception):
    """配備中の版の再生が壁時計上限を超えた (内部用)。"""


class _DeadlineSource:
    """配備中の版の intent source を包み、呼び出しのたびに壁時計上限を
    確かめる。再生の途中でも取り消せるようにするため。それ以外の属性は
    中の source へそのまま委譲する。"""

    def __init__(self, inner, deadline: float, clock: Callable[[], float]):
        self._inner = inner
        self._deadline = deadline
        self._clock = clock

    def __call__(self, closed_bar):
        if self._clock() >= self._deadline:
            raise _ReplayDeadline()
        return self._inner(closed_bar)

    def __getattr__(self, attr):
        # `_inner` が未設定の状態 (copy / unpickle) で自分自身を引き直して
        # 無限再帰しないよう、`_` 始まりは委譲しない。
        if attr.startswith("_"):
            raise AttributeError(attr)
        return getattr(self._inner, attr)


def _project(m: dict) -> dict:
    return {k: m.get(k) for k in _COMPARE_METRIC_KEYS}


def _build_comparison(*, pairs: list[str], stage: dict) -> dict:
    """`stage` は `{"candidate": {pair: 指標}, "deployed": {pair: 指標}}`。"""
    candidate, deployed = stage["candidate"], stage["deployed"]
    return {
        pair: {"candidate": _project(candidate[pair]),
               "deployed": _project(deployed[pair]),
               "verdict": compare_metrics(deployed[pair], candidate[pair])}
        for pair in pairs}


def _unavailable_comparison(mode: str, reason: str) -> dict:
    return {"mode": mode, "verdict": "unavailable", "reason": reason,
            "stages": {"holdout": {}, "in_sample": {}}}


def _decide_comparison(*, mode: str, pairs: list[str],
                       stages: dict) -> dict:
    """`stages` は `{"in_sample"|"holdout": {"candidate": {...},
    "deployed": {...}}}`。決定段は holdout。明確に劣る pair が 1 つでも
    あれば、他の pair が比較不能でも `not_improved` (比較不能が通す側に
    倒れて劣る候補を通さないため)。"""
    built = {
        "holdout": _build_comparison(pairs=pairs, stage=stages["holdout"]),
        "in_sample": _build_comparison(
            pairs=pairs, stage=stages["in_sample"])}
    verdicts = [built["holdout"][p]["verdict"] for p in pairs]
    if not verdicts:
        verdict, reason = "unavailable", "no_pairs"
    elif "not_improved" in verdicts:
        verdict, reason = "not_improved", "holdout_not_exceeds"
    elif "unavailable" in verdicts:
        base_short = any(
            not stages["holdout"]["deployed"][p].get("evaluable")
            for p in pairs)
        verdict = "unavailable"
        reason = ("baseline_not_evaluable" if base_short
                  else "candidate_not_evaluable")
    else:
        verdict, reason = "improved", "holdout_exceeds"
    return {"mode": mode, "verdict": verdict, "reason": reason,
            "stages": built}


def floor_settings_kv(gate_settings: "ImproveGateSettings") -> str:
    """[profitability-floor-fix] G1 (2026-09-13): 適用閾値 3 値
    (`min_pf`/`require_positive_avg_r`/`require_holdout_evaluable`、
    `ImproveGateSettings.snapshot()` 由来) を `key=value` 形式の 1 行に
    組み立てる唯一の場所。以前は `plugin/switch.py::_run_full_gate` の
    activity 行・例外メッセージが同じ 3 行を手書きで複製しており
    (`min_pf=`/`require_positive_avg_r=`/`require_holdout_evaluable=`、
    既存 pin F6-11a が厳密一致で読む形式)、改善ループの `gate_failed`
    activity 行 (G1) でも同じ文字列を組み立てる必要が生じたため、この
    唯一の場所に集約する。フォーマットは既存 pin (F6-11a) と厳密一致 —
    `ImproveGateSettings.snapshot()` の 3 キーの順序でスペース区切り。"""
    snap = gate_settings.snapshot()
    return " ".join(f"{key}={value}" for key, value in snap.items())


def floor_rule_text(gate_settings: "ImproveGateSettings", *,
                    audience: Literal["agent", "human"]) -> str:
    """[profitability-floor] codex 2 周目レビュー CR3 (2026-09-13):
    フロア条件を説明する文言の**条件節**を組み立てる唯一の場所。以前は
    `loops/improve_loop.py::_floor_rule_text` (LLM 向けプロンプト)・
    `backtest/cli.py::_plugin_bless::_warn` (CLI 警告)・
    `plugin/switch.py::_run_full_gate` (人間 corridor の ValueError/
    activity 文言) がそれぞれ独立に `min_pf`/`require_positive_avg_r`/
    `require_holdout_evaluable` を手書きで文言化しており、新しい閾値を
    足すたびに 3 箇所を手で揃える必要があった (1 箇所を書き換え忘れると
    CLI 操作者・LLM のプロンプト・activity ログが 3 つの異なるバージョンの
    説明を見ることになる)。

    戻り値は「条件節」(例: ``"`pf < 1.0` または `avg_r <= 0`"``) のみ —
    文全体ではないので、各呼び出し元は自分の文脈に合わせて前後に文言を
    足せる (CLI の警告文・improve_loop の規律文・switch.py の例外文で
    それぞれ文体が異なるため)。

    **`audience="agent"` (LLM 向け、改善ループのプロンプト) は
    `require_holdout_evaluable` を条件節に一切含めない** — 遮断 8
    (holdout の閾値・条件を agent に見せない設計判断、
    [profitability-floor] 設計書 §4 の「ただし書き」) を優先する。
    この設定が有効でも、agent 向け文言は pf/avg_r だけで説明する。
    holdout 起因の拒否は agent から見ると「in_sample 段のヒント
    (`submission_blocked`) は出なかったのに最終的に `unprofitable` に
    なった」という形になる — これは設計書 §4 が受容済みの挙動であり、
    ここで holdout 条件を漏らして解消してはならない (codex 2 周目
    レビュー CR1 の裁定)。

    **`audience="human"` (CLI/activity/例外メッセージ向け) は
    `require_holdout_evaluable=True` のとき holdout 条件も条件節に
    含める** — 人間はフロアの全条件を知る権利があり、遮断 8 の対象外
    (CR1)。"""
    g = gate_settings
    parts = [f"`pf < {g.min_pf}`"]
    if g.require_positive_avg_r:
        parts.append("`avg_r <= 0`")
    if audience == "human" and g.require_holdout_evaluable:
        parts.append("holdout が評価可能 (30 trades 以上) でない")
    return " または ".join(parts)


def evaluate_strategy_adoption_gate(
    conn, *, meta: "PluginMeta | None", now: datetime, settings: "Settings",
    resolved: "ResolvedIndicatorSet",
    inventory: "InventoryBuildResult | None" = None,
    name: str | None = None, pairs: "list[str] | None" = None,
    timeframe: str | None = None, content_hash: str | None = None,
    kind: str = "strategy",
    history_conn=None,
    run_in_sample_fn=None, run_holdout_gate_fn=None,
    record_fn: "Callable[[dict], None] | None" = None,
    floor_mode: Literal["enforce", "warn"] = "enforce",
    evaluation_sink: "Callable[[str, str, Any, BaseException | None], None] | None" = None,
    compare_mode: Literal["off", "shadow", "enforce"] = "off",
    baseline_replay_wall_clock_max_sec: float = 300.0,
    clock: Callable[[], float] = time.monotonic,
) -> "StrategyGateVerdict | None":
    """candidate/baseline/no_strategy の identity と評価可能性。
    indicator/signal はこのゲートを課さない (None を返す)。

    [indicator-consumption-wiring] T3 Step 3-1: `resolved` は呼び出し元
    (`run_kind_gate` / `ImproveLoop`) が composition root で 1 回だけ解決
    したもの。この関数は再解決しない — in_sample/holdout 両ループの
    `build_intent_source(...)` へ同じオブジェクトをそのまま渡す (P3')。

    **直前修正の申し送り④ (`run_in_sample`/`run_holdout_gate` の欠落引数)**:
    現物シグネチャ `run_in_sample(settings, *, history_conn, symbol, source,
    intent_source, eval_timeframe, plugin_ref, content_hash, kind, now,
    record_fn=None)` (7-D) は `settings`/`history_conn`/`intent_source` を
    必須で要求する。`history_conn` は明示指定が無ければ `conn` を再利用する
    (既存 `plugin/approval.py:_validate_strategy` と同じパターン — この
    改善ループの DB は単一ファイルであり、`backtest_runs`/`approval_requests`
    は同じ接続で読み書きできる)。`intent_source` は候補ごとに
    `strategy_adapter.build_intent_source(meta, conn=conn, pair=pair,
    source=settings.backtest.eval_source, settings=settings)` で組み立て、
    `_validate_strategy`
    と同じ try/finally で `close()` する (サンドボックスプロセスのリーク防止) —
    in-sample ループと holdout ループはそれぞれ独立に構築・close する
    (閉じた intent_source は使い回せない)。

    **`meta` は `intent_source` 構築のためだけに使う — identity/persist に
    使う `content_hash`/`timeframe` は常に明示引数 (10.6 節が再計算した値)
    が正**。`meta.content_hash`/`meta.timeframe` が引数と食い違っていても
    引数側が勝つ (10.6 節のハッシュ再照合の意図を保つ)。`kind != "strategy"`
    のときは `meta` を一切参照せず早期 return するため `meta=None` で呼べる。
    `eval_timeframe` は `plugin/approval.py:_eval_timeframe` と同じ写像
    (`"1d"` → `"24h"`) を明示引数 `timeframe` へ適用する — 適用しないと
    `bless`/`_validate_strategy` が書く行と `timeframe` 列が食い違い、
    §4.2-4 の baseline/candidate identity が崩れる。

    `record_fn` は `holdout.run_in_sample`/`run_holdout_gate` の
    non-committing sink (Task 7-D、`_run_scope` の `record_fn(save_kwargs)`
    契約 — 単一の dict を位置引数で渡す形。`ImproveLoop.commit` 手順4は
    ここへ `list.append` を渡し、蓄積した行を Tx-2 (`_persist_gate_rows`)
    へ渡す (設計書 §4.1「long-running work is outside tx」、3 周目レビュー
    Important-2)。

    [profitability-floor] codex 2 周目レビュー CR8 是正 (2026-09-13、
    stale docstring 訂正): **`record_fn=None` (即時 commit) の経路は
    現在どちらの corridor からも到達しない。** `plugin/approval.py::
    run_kind_gate` は改善ループ (`ImproveLoop.commit` 手順4) からも
    人間 corridor (`switch.py::submit_candidate`/`bless_candidate` →
    `_run_full_gate`) からも、**常に**明示的な sink (`record_fn=_sink`、
    T1 Step 1-6) を渡すようになった — 旧稿は「`bless --from _human` は
    Tx-2 の外から呼ぶので `None` 経路を使う」と書いていたが、これは
    T1 Step 1-6 (2026-09-13) 以前の実装を指しており、現在は
    `switch._run_full_gate` が捕捉した行を `outcome.gate_rows` 経由で
    自分の tx 内に明示的に persist する (`switch.py::
    _persist_human_gate_rows`/成功パスの gate-row 保存)。**この
    auto-commit 分岐は現状どちらの corridor からも呼ばれない到達不能
    コードであり、削除しないのは「将来 Tx-2 の外から `record_fn` 無しで
    呼ぶ第三の corridor ができたときの後方互換」のためだけ** —
    このコメントを信じて switch.py 側の明示 persist ループ (T1 Step
    1-6) を削除・変更すると、strategy 候補の in_sample/holdout 実測が
    無言で失われる (どちらの corridor も auto-commit に頼っていないため)。

    `compare_mode` が `shadow` / `enforce` のとき、配備中の同名版
    (`inventory` から引く) を候補と同じ `now`・dataset・分割で回して比べる。
    `inventory` が無いと配備中の版を引けないので `ValueError`。`off` は
    比較なし (従来どおり)。契約検査は床より前、再生は床通過の後。
    `baseline_replay_wall_clock_max_sec` は床通過直後に始まり、配備中の
    版の再生だけに掛かる (候補の床は打ち切らない)。配備中の版の行は
    `record_fn` へ流さず verdict の `baseline_rows` に載せて返す (再生が
    最後まで成功したときだけ。失敗・超過では空)。契約違反は
    `evaluable=False`、`floor_reason=same_name_contract_changed` で返す。"""
    if kind != "strategy":
        return None
    if compare_mode not in ("off", "shadow", "enforce"):
        raise ValueError(f"unknown compare_mode: {compare_mode!r}")
    if compare_mode != "off" and inventory is None:
        raise ValueError("compare_mode requires inventory")
    if baseline_replay_wall_clock_max_sec <= 0:
        raise ValueError("baseline_replay_wall_clock_max_sec must be > 0")
    dataset = settings.backtest.dataset()
    if dataset.unconfigured:
        return StrategyGateVerdict(
            evaluable=False,
            observation_reason=HISTORY_SOURCE_UNCONFIGURED_REASON,
            unevaluable_kind="history_source_unconfigured")
    # R-i3 追随 (プラン10 Task 11): `plugin/approval.py::run_kind_gate` は
    # 検証済みの `meta` (PluginMeta) だけを持って呼ぶ (switch.py の候補は
    # 直前に discover した鮮度の高い meta なので name/pairs/timeframe/
    # content_hash は meta 由来で正しい — 10.6 節の「明示引数が正」の懸念は
    # ImproveLoop.commit のように meta が使い回されて古くなり得る経路の話
    # であり、switch.py の一発ゲートには当てはまらない)。明示引数が渡され
    # ればそちらを常に優先する (10.6 節の契約は維持)。
    name = meta.name if name is None else name
    pairs = list(meta.pairs) if pairs is None else pairs
    timeframe = meta.timeframe if timeframe is None else timeframe
    content_hash = meta.content_hash if content_hash is None else content_hash
    history_conn = conn if history_conn is None else history_conn
    run_in_sample = run_in_sample_fn or holdout.run_in_sample
    run_holdout = run_holdout_gate_fn or holdout.run_holdout_gate
    plugin_ref = f"plugins/{name}"
    eval_timeframe = _eval_timeframe(timeframe)

    # baseline の要否 (approved 同名 strategy の有無) は per_pair の結果に
    # 依存しない純粋な参照なので、in-sample ループより前に確定できる。
    # ここで確定させておくことで、no_strategy になる場合だけ record_fn を
    # 捕捉ラッパーへ差し替える (approved-baseline のときは呼び出し元の
    # record_fn をそのまま転送する既存契約を保つ — 3 周目レビュー
    # Important-2 の pin `test_record_fn_is_forwarded_to_run_in_sample_
    # and_run_holdout` が「record_fn はそのまま転送される」ことを要求する
    # ため)。
    # [indicator-consumption-wiring] §2.7: baseline 判定は
    # `ApprovedInventory` の strategy 集合を正本にする — approval_requests
    # を直接引くと、pin 破れで配備から外れた同名 strategy を baseline と
    # 誤認する (それは既に live で動いていない)。`inventory` が渡されない
    # 経路 (テストの直接呼び出し) は従来の SQL にフォールバックする。
    if inventory is not None:
        has_approved_baseline = any(
            m.name == name and m.kind == "strategy"
            for m in inventory.inventory.metas)
    else:
        has_approved_baseline = conn.execute(
            "SELECT 1 FROM approval_requests WHERE kind='plugin' "
            "AND status='approved' "
            "AND json_extract(payload_json,'$.name')=? "
            "AND json_extract(payload_json,'$.kind')='strategy' "
            "LIMIT 1", (name,)).fetchone() is not None

    from agentic_fx.store import backtest_runs as backtest_runs_store

    deployed_meta = None
    if compare_mode != "off":
        deployed_meta = next(
            (m for m in inventory.inventory.metas
             if m.name == name and m.kind == "strategy"), None)
    if deployed_meta is not None and (
            deployed_meta.timeframe != timeframe
            or set(deployed_meta.pairs) != set(pairs)):
        # 既存の呼び出し側は `evaluable` で fail closed になる。
        return StrategyGateVerdict(
            evaluable=False, baseline_variant="baseline",
            baseline_row={"ref_plugin_ref": plugin_ref, "variant": "baseline"},
            floor_reason=SAME_NAME_CONTRACT_CHANGED,
            contract_violation=SAME_NAME_CONTRACT_CHANGED)

    # no_strategy baseline (§4.2-4): 候補の in-sample 行 (pair ごと) の
    # identity (period/settings_hash/core_commit/initial_balance/source/
    # timeframe) をそのまま複製し、plugin_ref/variant/metrics だけ
    # no_strategy 用に差し替えて record_fn/Tx-2 の既存経路 (record_fn is
    # None のときは即時 save、`holdout._run_scope` と同じ形) へ流す。
    # `baseline_row` (approval payload 添付用の JSON-safe 4 key dict) とは
    # 別物 — `period` に datetime を含む行を payload に混ぜると
    # `approvals_store.create` の json.dumps で TypeError になる。
    in_sample_rows: list[dict] = []
    if not has_approved_baseline:
        def _in_sample_record_fn(row: dict) -> None:
            in_sample_rows.append(row)
            if record_fn is not None:
                record_fn(row)
            else:
                backtest_runs_store.save_harness_run(history_conn, **row)
    else:
        _in_sample_record_fn = record_fn

    # 評価 1 回ごとに (scope, pair, intent_source, 失敗した例外 | None) を
    # 渡す。失敗で例外が伝播する場合も、worker に触れた評価の診断は
    # verdict に載らないため、この sink でしか呼び出し元へ届かない。
    def _notify(scope: str, pair: str, source: Any,
                error: BaseException | None) -> None:
        if evaluation_sink is None:
            return
        # 通知の失敗は評価結果 (成功の verdict、または失敗の元の例外) を
        # 隠してはならない。finally から呼ばれるので握ってログに残す。
        try:
            evaluation_sink(scope, pair, source, error)
        except Exception:
            _log.warning("evaluation sink failed scope=%s pair=%s",
                         scope, pair, exc_info=True)

    cpu_samples: list[tuple[str, str, float | None]] = []
    per_pair = {}
    for pair in pairs:
        intent_source = strategy_adapter.build_intent_source(
            meta, conn=conn, pair=pair, dataset=dataset,
            settings=settings, resolved=resolved)
        error: BaseException | None = None
        try:
            per_pair[pair] = run_in_sample(
                settings, history_conn=history_conn, symbol=pair,
                dataset=dataset, intent_source=intent_source,
                eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
                content_hash=content_hash, kind="strategy", now=now,
                record_fn=_in_sample_record_fn)
        except Exception as exc:
            error = exc
            raise
        finally:
            intent_source.close()
            # [indicator-consumption-wiring] §2.9(e): close 完了後に確定する
            # property。例外終了時も必ず 1 件積む (cpu_sec=None)。
            cpu_samples.append(("in_sample", pair, intent_source.cpu_sec))
            _notify("in_sample", pair, intent_source, error)
    total_trades = sum(m["trades"] for m in per_pair.values())
    evaluable = total_trades >= EVALUABLE_MIN_TRADES
    if not evaluable:
        return StrategyGateVerdict(
            evaluable=False,
            observation_reason=f"insufficient_trades:{total_trades}",
            cpu_samples=tuple(cpu_samples))

    # [profitability-floor] T1 Step 1-2 (2026-09-12、設計書 §3):
    # in_sample 段の判定は total_trades 判定の直後 (holdout ループより前)。
    # `floor_mode="enforce"` で不合格なら holdout を回さずに即 return する
    # (落ちる候補に holdout の計算コストを払わない/holdout 情報の発生
    # 自体を無くす — 遮断 8 の 1 bit 例外の前提)。`floor_mode="warn"` の
    # 不合格は `floor_reason`/`floor_detail` を保持したまま完走する
    # (codex C2 — bless で強行する候補にも通常 submit と同じ全ゲート
    # 証跡を残すため)。
    in_sample_floor_reason, in_sample_floor_detail = _check_profitability_floor(
        per_pair, settings=settings, scope="in_sample")
    if in_sample_floor_reason and floor_mode == "enforce":
        return StrategyGateVerdict(
            evaluable=True, floor_reason=in_sample_floor_reason,
            floor_detail=in_sample_floor_detail, candidate_metrics=per_pair,
            cpu_samples=tuple(cpu_samples))

    holdout_per_pair: dict = {}
    for pair in pairs:
        intent_source = strategy_adapter.build_intent_source(
            meta, conn=conn, pair=pair, dataset=dataset,
            settings=settings, resolved=resolved)
        error = None
        try:
            # 現行は戻り値を捨てていた (§0「前提を疑う」) — フロア判定の
            # holdout 段はこの戻り値が要る。
            holdout_per_pair[pair] = run_holdout(
                settings, history_conn=history_conn, symbol=pair,
                dataset=dataset, intent_source=intent_source,
                eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
                content_hash=content_hash, kind="strategy", now=now,
                record_fn=record_fn)
        except Exception as exc:
            error = exc
            raise
        finally:
            intent_source.close()
            cpu_samples.append(("holdout", pair, intent_source.cpu_sec))
            _notify("holdout", pair, intent_source, error)

    holdout_floor_reason, holdout_floor_detail = _check_profitability_floor(
        holdout_per_pair, settings=settings, scope="holdout")
    # warn で in_sample も落ちていた場合は floor_detail に両段を含める。
    floor_reason = in_sample_floor_reason or holdout_floor_reason
    floor_detail = " | ".join(
        d for d in (in_sample_floor_detail, holdout_floor_detail) if d)

    if has_approved_baseline:
        baseline_row = {"ref_plugin_ref": plugin_ref, "variant": "baseline"}
        comparison = None
        failure_code = ""
        replay_rows: list[dict] = []
        if deployed_meta is not None and not floor_reason:
            baseline_row["base_content_hash"] = deployed_meta.content_hash
            comparison, failure_code, replay_rows = _compare_with_deployed(
                deployed_meta, inventory=inventory, mode=compare_mode,
                conn=conn, history_conn=history_conn, settings=settings,
                dataset=dataset, pairs=list(pairs), now=now,
                plugin_ref=plugin_ref, candidate_hash=content_hash,
                eval_timeframe=eval_timeframe,
                candidate={"in_sample": per_pair, "holdout": holdout_per_pair},
                run_in_sample=run_in_sample, run_holdout=run_holdout,
                notify=_notify, cpu_samples=cpu_samples,
                max_sec=baseline_replay_wall_clock_max_sec, clock=clock)
        return StrategyGateVerdict(
            evaluable=True, baseline_variant="baseline",
            baseline_row=baseline_row, candidate_metrics=per_pair,
            floor_reason=floor_reason, floor_detail=floor_detail,
            holdout_metrics=holdout_per_pair, cpu_samples=tuple(cpu_samples),
            comparison=comparison,
            not_improved=bool(
                comparison and comparison["verdict"] == "not_improved"),
            baseline_failure_code=failure_code,
            baseline_rows=tuple(replay_rows))

    for row in in_sample_rows:
        no_strategy_row = dict(row)
        no_strategy_row["plugin_ref"] = f"no_strategy:{name}"
        no_strategy_row["variant"] = "no_strategy"
        no_strategy_row["metrics"] = {
            "trades": 0, "pf": None, "win_rate": None, "avg_r": None,
            "max_drawdown": 0.0, "total_pnl": 0.0, "evaluable": False,
            "fallback_spread_used": False}
        if record_fn is not None:
            record_fn(no_strategy_row)
        else:
            backtest_runs_store.save_harness_run(
                history_conn, **no_strategy_row)

    baseline_row = {"plugin_ref": f"no_strategy:{name}",
                    "content_hash": content_hash, "kind": "strategy",
                    "variant": "no_strategy"}
    return StrategyGateVerdict(
        evaluable=True, baseline_variant="no_strategy",
        baseline_row=baseline_row, candidate_metrics=per_pair,
        floor_reason=floor_reason, floor_detail=floor_detail,
        holdout_metrics=holdout_per_pair, cpu_samples=tuple(cpu_samples))


def _compare_with_deployed(
        deployed: PluginMeta, *, inventory, mode: str, conn, history_conn,
        settings, dataset, pairs: list[str], now: datetime, plugin_ref: str,
        candidate_hash: str, eval_timeframe: str, candidate: dict,
        run_in_sample, run_holdout, notify, cpu_samples: list,
        max_sec: float,
        clock: Callable[[], float]) -> tuple[dict, str, list[dict]]:
    """配備中の版を pair ごとに in_sample → holdout で回し、比較を返す。
    `candidate` は `{"in_sample": {pair: 指標}, "holdout": {...}}`。
    戻り値は `(comparison, failure_code, baseline_rows)`。

    行は `record_fn` へ流さず、最後まで成功したときだけ返す (失敗・超過では
    空 = 候補の行だけが残る)。`sandbox_unavailable` と未知の code、
    `SandboxError` 以外の例外はそのまま伝播する (呼び出し側が mission を
    failed にする)。壁時計は各呼び出しの前に確かめる (完走した結果は
    捨てない。1 回の呼び出しの固まりは worker の timeout に委ねる)。"""
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet

    resolved = inventory.resolved.get((deployed.name, deployed.content_hash))
    if resolved is None:
        if deployed.indicators:
            return _unavailable_comparison(mode, "baseline_unhealthy"), "", []
        resolved = ResolvedIndicatorSet.empty(inventory.inventory.root)

    deadline = clock() + max_sec
    staged: list[dict] = []

    def _stage(row: dict) -> None:
        staged.append({**row, "variant": "baseline",
                       "ref_plugin_ref": plugin_ref,
                       "ref_content_hash": candidate_hash})

    deployed_metrics: dict[str, dict] = {"in_sample": {}, "holdout": {}}
    try:
        for pair in pairs:
            for scope, run_fn in (("in_sample", run_in_sample),
                                  ("holdout", run_holdout)):
                if clock() >= deadline:
                    raise _ReplayDeadline()
                source = strategy_adapter.build_intent_source(
                    deployed, conn=conn, pair=pair, dataset=dataset,
                    settings=settings, resolved=resolved)
                error: BaseException | None = None
                try:
                    deployed_metrics[scope][pair] = run_fn(
                        settings, history_conn=history_conn, symbol=pair,
                        dataset=dataset,
                        intent_source=_DeadlineSource(source, deadline, clock),
                        eval_timeframe=eval_timeframe, plugin_ref=plugin_ref,
                        content_hash=deployed.content_hash, kind="strategy",
                        now=now, record_fn=_stage)
                except Exception as exc:
                    error = exc
                    raise
                finally:
                    source.close()
                    cpu_samples.append(
                        (f"baseline_{scope}", pair, source.cpu_sec))
                    # 上限超過はこの gate の正常な unavailable なので、
                    # 失敗として sink へは通知しない。
                    if not isinstance(error, _ReplayDeadline):
                        notify(f"baseline_{scope}", pair, source, error)
    except _ReplayDeadline:
        return (_unavailable_comparison(mode, "gate_timeout"),
                "gate_timeout", [])
    except SandboxError as exc:
        kind = classify_replay_failure(exc.code)
        if kind != "unavailable":
            raise
        return (_unavailable_comparison(mode, "baseline_unhealthy"),
                exc.code, [])

    stages = {scope: {"candidate": candidate[scope],
                      "deployed": deployed_metrics[scope]}
              for scope in ("in_sample", "holdout")}
    return (_decide_comparison(mode=mode, pairs=pairs, stages=stages),
            "", staged)
