"""plugin 承認フロー (kind=plugin) + bless (プラン 7 Task 6、設計書 §6)。

`approval_requests` (`kind="plugin"`) への提出 (`submit_plugin`) と、CLI
専用の即時承認 (`bless`) を提供する。承認フローの consumer としての立場
から、下流モジュール (`sandbox.check_source`/`signal_eval.
evaluate_detection`/`strategy_adapter`/`holdout.run_in_sample`) が意図的
に catch していない `SandboxError` をここで初めて catch し、`ValueError`
へ統一する (下流の docstring が明言する「fail closed で承認フロー
(Task 6 の consumer) に伝える」の実装箇所がここ)。

**検証順序 (brief 逐語 + レビュー fix、fail closed)**:
⓪ `meta.max_bars <= settings.plugin.max_bars_limit` 照合 (最終レビュー
F1) → ① `check_source(plugin.py)` → ② `check_source(test_plugin.py,
extra_allowed={"pytest", "plugin"})` → pytest 実行 → ③ kind 別検証
(indicator: 何もしない / signal: `evaluate_detection` / strategy:
`meta.pairs` 全数を `settings.pairs` と照合してから各 pair の
`run_in_sample`) → ④ content_hash 再検証 (discover 時の `meta.
content_hash` と検証完了時点のファイル内容が一致することを確認 — TOCTOU
封鎖、レビュー fix round 1 F1) → ⑤ `approvals.create`。⓪〜④ のいずれの
段階が失敗しても `approval_requests` 行は作らない (例外はすべて
`ValueError` に統一 — `approvals.create` 自体の失敗 (DB エラー等) だけは
変換せず素通しする — 変換対象は「plugin 検証の失敗」に限る)。

**承認 source と本番 source の差異を人間に見せる (opus R2 I1)**: payload
の `eval_source` は承認バックテストが常に使う `"dukascopy"` 固定、
`live_source` は `settings.plugin.producer_source` (本番 producer が使う
source) — 両者が異なり得ることを承認レビュー時に人間が見えるようにする。

**evaluable の意味**: indicator/signal は `True` 固定 (schema 安定のため
キー自体は必ず含める — 精度評価は `signal_eval` 側の precision/recall で
別途行う)。strategy だけが実判定で、`meta.pairs` 全体の `trades` 合計が
`metrics.EVALUABLE_MIN_TRADES` (30) 以上かどうかを見る。pair ごとの
`run_in_sample` 戻り値にも `"evaluable"` キー (単一 pair 換算) が入って
いるが、これとは別物 — 混同しないこと。

**bless (D3)**: `submit_plugin` と同一の検証を実行し、成功したら
`approvals.create` の直後に `decide(status="approved",
decided_by="human_cli")` する。`submit_plugin` を呼んで id を得てから
`decide` するだけで、検証ロジックを二重に持たない。CLI からのみ呼ぶ
(改善ループの tool 定義には絶対に載せない — 回帰ピンは Task 9 の関心)。

**脅威モデル (Task 2 実装後)**: `test_plugin.py` の pytest は別プロセス
(`agentic_fx.plugin.pytest_sandbox_entry` 経由) で実行され、最小 env
(PATH/PYTHONPATH/PYTHONSAFEPATH + シングルスレッド化変数のみ)、resource
limit (RLIMIT_AS/NOFILE/FSIZE)、ネットワークモジュール毒入れが適用される。
完全な OS 隔離ではなく、AST ゲート (`sandbox.check_source`) は import 文・
denylist 名の**主要な迂回経路**を塞ぐが動的な名前組み立ては防げない。
したがって `submit_plugin`/`bless` は **自分自身または信頼できるソースが
書いた plugin に対してのみ実行すること** — 出所不明な plugin をそのまま
submit/bless する運用は想定していない (悪意ある入力に対する完全な隔離を
保証するのではなく、善意だが不注意な plugin の事故防止を目的とする)。
"""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agentic_fx.backtest import holdout
from agentic_fx.backtest.metrics import EVALUABLE_MIN_TRADES
from agentic_fx.plugin import strategy_adapter, strategy_gate
from agentic_fx.plugin.gate_pytest import GateResult, run_gate_pytest
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.loader import content_hash as _recompute_content_hash
from agentic_fx.plugin.sandbox import SandboxError, check_source
from agentic_fx.plugin.signal_eval import SandboxRunFn, evaluate_detection
from agentic_fx.store import approvals as approvals_store

if TYPE_CHECKING:
    from agentic_fx.config import Settings

# pytest_runner 注入シームの型: plugin ディレクトリ → GateResult。
PytestRunnerFn = Callable[[Path], GateResult]

# run_in_sample_fn 注入シームの型: holdout.run_in_sample と同じキーワード
# 専用シグネチャで呼ばれる (settings は位置引数)。
RunInSampleFn = Callable[..., dict[str, Any]]

_EVAL_SOURCE = "dukascopy"
_NOTE = "バックテスト成績は実運用成績の予測値ではない (足切り専用)"
# D5: plugin 宣言 timeframe → run_in_sample に渡す eval_timeframe。"1d" だけ
# "24h" へ写像する (runner.parse_timeframe が "1d" を受理しないため)。
_EVAL_TIMEFRAME_OVERRIDE = {"1d": "24h"}


def _eval_timeframe(meta_timeframe: str) -> str:
    return _EVAL_TIMEFRAME_OVERRIDE.get(meta_timeframe, meta_timeframe)


def _pytest_summary(stdout_text: str) -> str:
    """pytest の出力から末尾の非空行 (概ね summary 行) だけを抜き出す。"""
    lines = [line for line in stdout_text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _validate_strategy(conn: sqlite3.Connection, meta: PluginMeta, *,
                       settings: "Settings", now: datetime,
                       run_in_sample_fn: RunInSampleFn | None,
                       ) -> tuple[dict[str, dict], bool]:
    """strategy kind の検証: pairs 全数を settings.pairs と照合してから
    (1 pair でも外れていれば ValueError — バックテストは 1 回も実行しない)、
    各 pair について build_intent_source → run_in_sample → try/finally で
    close() する (Task 5 の CLI と同じ規律)。
    """
    invalid_pairs = [p for p in meta.pairs if p not in settings.pairs]
    if invalid_pairs:
        raise ValueError(
            f"plugin {meta.name!r}: pairs not in settings.pairs: "
            f"{invalid_pairs} (settings.pairs={list(settings.pairs)})")

    run_in_sample = (run_in_sample_fn if run_in_sample_fn is not None
                     else holdout.run_in_sample)
    eval_timeframe = _eval_timeframe(meta.timeframe)
    plugin_ref = f"plugins/{meta.name}"

    per_pair_metrics: dict[str, dict] = {}
    for pair in meta.pairs:
        intent_source = strategy_adapter.build_intent_source(
            meta, conn=conn, pair=pair, source=_EVAL_SOURCE, settings=settings)
        try:
            per_pair_metrics[pair] = run_in_sample(
                settings, history_conn=conn, symbol=pair, source=_EVAL_SOURCE,
                intent_source=intent_source, eval_timeframe=eval_timeframe,
                plugin_ref=plugin_ref, content_hash=meta.content_hash,
                kind="strategy", now=now)
        finally:
            # 上書き節 3 (Task 5 の CLI と同じ規律): run_replay が例外で
            # 終わってもサンドボックスプロセスをリークさせない。
            intent_source.close()

    total_trades = sum(m["trades"] for m in per_pair_metrics.values())
    evaluable = total_trades >= EVALUABLE_MIN_TRADES
    return per_pair_metrics, evaluable


def _validate_kind(conn: sqlite3.Connection, meta: PluginMeta, *,
                   settings: "Settings", now: datetime,
                   sandbox_run: SandboxRunFn | None,
                   run_in_sample_fn: RunInSampleFn | None,
                   ) -> tuple[dict, bool]:
    if meta.kind == "indicator":
        return {}, True
    if meta.kind == "signal":
        metrics = evaluate_detection(meta, sandbox_run=sandbox_run, settings=settings)
        return metrics, True
    if meta.kind == "strategy":
        return _validate_strategy(conn, meta, settings=settings, now=now,
                                  run_in_sample_fn=run_in_sample_fn)
    raise ValueError(f"plugin {meta.name!r}: unsupported kind {meta.kind!r}")


# precheck 2026-08-22 wave2: T11-B-11d
def run_kind_gate(conn: sqlite3.Connection, meta: PluginMeta, *,
                  settings: "Settings", now: datetime,
                  sandbox_run: SandboxRunFn | None = None,
                  run_in_sample_fn: RunInSampleFn | None = None,
                  ) -> tuple[dict, bool]:
    """`switch.py` の `submit_candidate`/`bless_candidate` (`_run_full_gate`
    手順 7) から共有呼び出しされる kind 別検証の入口。indicator/signal は
    `_validate_kind` (= `submit_plugin` の旧 kind="plugin" 直接申請 API と
    共有する検証ロジック) にそのまま委譲する — その経路は不変。

    kind=strategy だけは R-i3 (統合裁定) どおり `plugin/strategy_gate.py::
    evaluate_strategy_adoption_gate` を**唯一の実装**として import 共有する
    (`_validate_kind`/`_validate_strategy` の `holdout.run_in_sample` 直呼び
    経路は使わない — `_validate_strategy` 自体は `submit_plugin`
    (`backtest/cli.py` の `afx plugin submit` が使う旧 kind="plugin" API、
    switch.py の候補フローとは別corridor) からまだ直接使われているため削除
    しない。二重実装ではなく、2 つの独立した corridor がそれぞれ自分の
    ゲート実装を持っている — switch.py 側は本関数経由で必ず
    `evaluate_strategy_adoption_gate` を通る、というのがここでの pin)。

    `evaluate_strategy_adoption_gate` へは検証済みの `meta` だけを渡す —
    name/pairs/timeframe/content_hash は明示せず `meta` 由来のデフォルトに
    委ねる (switch.py 側は `_run_full_gate` が直前に discover した鮮度の
    高い meta を渡すため、10.6 節が懸念する staleness は生じない)。
    `evaluable=False` (閾値未満) は §8.1-41 pin どおり approval 行を一切
    作らせないため、ここで fail closed に ValueError へ倒す (verdict が
    そのまま `ValueError` を送出する場合 — 例えば history 不足 — はそれを
    そのまま伝播させる、二重に包まない)。`run_in_sample_fn` seam は
    switch.py 側にまだ注入経路が無い (既存テストは `submit_plugin`/
    `_validate_kind` 経由の corridor でしか使っていない — `grep -rn
    run_in_sample_fn tests/` で確認済み) ため、`evaluate_strategy_adoption_
    gate` の `run_in_sample_fn` 引数へは転送しない (シグネチャの
    `run_in_sample_fn` 仮引数は `_validate_kind` 経由の indicator/signal/
    非-strategy 呼び出しとの互換のためだけに残す)。
    """
    if meta.kind == "strategy":
        verdict = strategy_gate.evaluate_strategy_adoption_gate(
            conn, meta=meta, settings=settings, now=now)
        if verdict is None or not verdict.evaluable:
            reason = verdict.observation_reason if verdict is not None else ""
            raise ValueError(
                f"plugin {meta.name!r}: strategy not evaluable "
                f"({reason})")
        return (verdict.candidate_metrics or {}), verdict.evaluable
    return _validate_kind(conn, meta, settings=settings, now=now,
                          sandbox_run=sandbox_run, run_in_sample_fn=run_in_sample_fn)


def submit_plugin(conn: sqlite3.Connection, meta: PluginMeta, *,
                  settings: "Settings", now: datetime,
                  pytest_runner: PytestRunnerFn | None = None,
                  sandbox_run: SandboxRunFn | None = None,
                  run_in_sample_fn: RunInSampleFn | None = None) -> int:
    """plugin (kind=plugin) の承認申請行を作り、その id を返す。

    どの検証ゲート (max_bars_limit/check_source/pytest/kind 別検証/
    content_hash 再検証) が失敗しても `approval_requests` 行は作らない
    (fail closed)。
    `SandboxError` および検証ゲート自身が送出する `ValueError` はすべて
    `ValueError` に正規化して送出する (承認フローの consumer としての
    統一契約 — F5)。**環境障害 (`OSError`・`sqlite3.Error` 等、例えば
    `approvals.create` の DB 書き込み失敗) はここでは catch せず、その
    まま貫通させる** — 変換対象は「plugin 検証の失敗」に限る。
    """
    test_plugin_path = meta.path / "test_plugin.py"
    # test_file_hash は監査値 (ロード時検証には使わない)。ゲートを通る前の
    # バイト列を記録する — ゲート後の内容で記録すると、pytest 実行から
    # ハッシュ算出までの間に (理論上のレースで) 差し替えられた内容を承認
    # したことになりかねない。なお check_source は別途この後ファイルを
    # 再読み込みするため、両読み取りの間に ms 級のレースが理論上残る
    # (sandbox.py の PluginSession.__enter__ が明記する脅威モデルと同じ
    # 許容範囲 — 悪意ある攻撃者からの完全な隔離は保証しない)。
    test_plugin_bytes = test_plugin_path.read_bytes()
    test_file_hash = hashlib.sha256(test_plugin_bytes).hexdigest()

    try:
        # F1 (最終レビュー — Fable/codex 両方一致、Important): max_bars_limit
        # の照合は「消費側の責務」(config.py の docstring) だが、これまで
        # 照合していたのは market_tools.get_indicators (indicator 合成)
        # だけだった。承認 (submit/bless)・producer・CLI --plugin はどこも
        # 照合しておらず、`max_bars: 500000` のような strategy/signal
        # plugin が承認バックテストの毎バケットで全履歴読みを起こし得た
        # (producer は資金保護処理より前に走るため tick 遅延の実害がある)。
        # ここで検証ゲート冒頭に一律 fail closed で置くことで、bless は
        # submit 経由なので自動的に守られ、producer は承認済み plugin しか
        # 受け付けないため承認のこの 1 点だけで律速できる。
        if meta.max_bars > settings.plugin.max_bars_limit:
            raise ValueError(
                f"plugin {meta.name!r}: max_bars {meta.max_bars} exceeds "
                f"settings.plugin.max_bars_limit {settings.plugin.max_bars_limit}")

        check_source(meta.path / "plugin.py")
        check_source(test_plugin_path, extra_allowed=frozenset({"pytest", "plugin"}))

        runner = (pytest_runner if pytest_runner is not None
                  else lambda d: run_gate_pytest(d, settings=settings))
        pytest_result = runner(meta.path)
        # <!-- precheck 2026-08-22: T6-B1 --> fail closed: `passed` を正
        # として読む (`returncode` だけを見ると、`run_gate_pytest` が
        # 候補 hash 不一致で返す `GateResult(passed=False,
        # returncode=<pytest の実 returncode>)` — テスト自体は緑なら
        # returncode は 0 — や timeout (returncode=-1 の場合はこれまでも
        # 検出できていたが、たまたま returncode=0 が返る変異形では検出
        # できない) を見落とし、承認申請行を作ってしまう (fail open))。
        if not pytest_result.passed:
            raise ValueError(
                f"plugin {meta.name!r}: test_plugin.py failed pytest "
                f"(returncode={pytest_result.returncode}): "
                f"{_pytest_summary(pytest_result.stdout_tail)}")

        metrics, evaluable = _validate_kind(
            conn, meta, settings=settings, now=now, sandbox_run=sandbox_run,
            run_in_sample_fn=run_in_sample_fn)

        # F1 (codex Critical レビュー fix round 1): 全検証通過後・
        # approvals.create の直前に content_hash を再計算し、discover 時に
        # 取得した meta.content_hash と照合する (TOCTOU 封鎖)。stale hash
        # swap シナリオ: discover で悪性版を拾う → 検証前に良性版へ差し替え
        # → check_source/pytest は良性版で通過 → 悪性版に戻す → payload に
        # 悪性版のハッシュが載り、tools/plugin_loader が悪性版を承認済みと
        # して受理してしまう。signal/strategy は評価中に
        # `sandbox.PluginSession.__enter__` の再検証に掛かるが、indicator
        # は plugin.py を一切実行しないため無防備だった — kind に依らず
        # ここで一律に再検証する。ms 級の残余レース (この再計算直後に再度
        # 差し替えられる) は `sandbox.py` の脅威モデルと同じ許容範囲。
        current_hash = _recompute_content_hash(meta.path)
        if current_hash != meta.content_hash:
            raise ValueError(
                f"plugin {meta.name!r}: content changed since discovery "
                "(hash mismatch) — refusing to approve "
                f"(expected {meta.content_hash}, got {current_hash})")
    except SandboxError as exc:
        raise ValueError(f"plugin {meta.name!r}: {exc}") from exc

    payload = {
        "name": meta.name,
        "kind": meta.kind,
        "content_hash": meta.content_hash,
        "test_file_hash": test_file_hash,
        "pytest": {"returncode": pytest_result.returncode,
                   "summary": _pytest_summary(pytest_result.stdout_tail)},
        "metrics": metrics,
        "evaluable": evaluable,
        "eval_source": _EVAL_SOURCE,
        "live_source": settings.plugin.producer_source,
        "note": _NOTE,
    }
    # kind="plugin" は approval_requests.kind の値 (この承認要求が「plugin
    # の承認」であることを表す — PluginMeta.kind (indicator/signal/
    # strategy) とは別の軸。tools/plugin_loader.py の docstring と同じ区別)。
    # expires_at は None (plugin 承認に期限は無い — brief 非言及)。
    return approvals_store.create(conn, kind="plugin", payload=payload, now=now)


def bless(conn: sqlite3.Connection, meta: PluginMeta, *, settings: "Settings",
         now: datetime, pytest_runner: PytestRunnerFn | None = None,
         sandbox_run: SandboxRunFn | None = None,
         run_in_sample_fn: RunInSampleFn | None = None) -> int:
    """裁定3 (プラン10 Task11g): live path (`plugins/<name>`) を候補に取る
    bless は廃止された。live/plain の版ではなく `plugins/_human/<name>` を
    候補に取る `switch.bless_candidate` (P3、`afx plugin bless --from _human`
    経由) が唯一の bless 経路 — この旧 API は常に拒否する (materialize +
    `bless --from _human` を案内するエラー)。"""
    raise ValueError(
        "afx plugin bless <name> は廃止されました。"
        "'afx plugin materialize <name>' で候補を書き出し、編集してから "
        "'afx plugin bless --from _human <name>' を実行してください。")
