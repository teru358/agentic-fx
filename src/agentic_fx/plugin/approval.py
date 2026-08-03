"""plugin 承認フロー (kind=plugin) + bless (プラン 7 Task 6、設計書 §6)。

`approval_requests` (`kind="plugin"`) への提出 (`submit_plugin`) と、CLI
専用の即時承認 (`bless`) を提供する。承認フローの consumer としての立場
から、下流モジュール (`sandbox.check_source`/`signal_eval.
evaluate_detection`/`strategy_adapter`/`holdout.run_in_sample`) が意図的
に catch していない `SandboxError` をここで初めて catch し、`ValueError`
へ統一する (下流の docstring が明言する「fail closed で承認フロー
(Task 6 の consumer) に伝える」の実装箇所がここ)。

**検証順序 (brief 逐語、fail closed)**:
① `check_source(plugin.py)` → ② `check_source(test_plugin.py,
extra_allowed={"pytest", "plugin"})` → pytest 実行 → ③ kind 別検証
(indicator: 何もしない / signal: `evaluate_detection` / strategy:
`meta.pairs` 全数を `settings.pairs` と照合してから各 pair の
`run_in_sample`) → ④ `approvals.create`。①〜③ のいずれの段階が失敗して
も `approval_requests` 行は作らない (例外はすべて `ValueError` に統一 —
`approvals.create` 自体の失敗 (DB エラー等) だけは変換せず素通しする —
変換対象は「plugin 検証の失敗」に限る)。

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
"""
from __future__ import annotations

import hashlib
import subprocess
import sys
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agentic_fx.backtest import holdout
from agentic_fx.backtest.metrics import EVALUABLE_MIN_TRADES
from agentic_fx.plugin import strategy_adapter
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.plugin.sandbox import SandboxError, check_source
from agentic_fx.plugin.signal_eval import SandboxRunFn, evaluate_detection
from agentic_fx.store import approvals as approvals_store

if TYPE_CHECKING:
    from agentic_fx.config import Settings

# pytest_runner 注入シームの型: test_plugin.py の絶対パス → 少なくとも
# {"returncode": int, "stdout": str} を持つ dict。
PytestRunnerFn = Callable[[Path], dict[str, Any]]

# run_in_sample_fn 注入シームの型: holdout.run_in_sample と同じキーワード
# 専用シグネチャで呼ばれる (settings は位置引数)。
RunInSampleFn = Callable[..., dict[str, Any]]

_EVAL_SOURCE = "dukascopy"
_NOTE = "バックテスト成績は実運用成績の予測値ではない (足切り専用)"
# D5: plugin 宣言 timeframe → run_in_sample に渡す eval_timeframe。"1d" だけ
# "24h" へ写像する (runner._parse_timeframe が "1d" を受理しないため)。
_EVAL_TIMEFRAME_OVERRIDE = {"1d": "24h"}

# 既定 pytest 実行の待ち上限。sandbox.py の call() 用 sandbox_timeout_sec
# (既定 10s) とは別枠 — こちらは test_plugin.py 一式 (pandas import 含む)
# を実行するため、暴走 test_plugin.py に対する独自のタイムアウト防御を持つ
# (`_STARTUP_TIMEOUT_SEC` 同様の「別の関心事には別の定数」原則)。
_PYTEST_TIMEOUT_SEC = 300.0


def _eval_timeframe(meta_timeframe: str) -> str:
    return _EVAL_TIMEFRAME_OVERRIDE.get(meta_timeframe, meta_timeframe)


def _pytest_summary(stdout_text: str) -> str:
    """pytest の出力から末尾の非空行 (概ね summary 行) だけを抜き出す。"""
    lines = [line for line in stdout_text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _default_pytest_runner(test_plugin_path: Path) -> dict[str, Any]:
    """既定の pytest 実行シーム。

    **1 plugin につき 1 サブプロセス** で実行する (サンプル test_plugin.py
    のモジュール名衝突回避 — 複数 plugin の test_plugin.py をまとめて 1 回
    の pytest 呼び出しに乗せない、という確立済みの申し送り)。
    `-p no:cacheprovider` で plugin フォルダに `.pytest_cache` を作らせな
    い (キャッシュ汚染回避)。
    """
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
             str(test_plugin_path)],
            capture_output=True, text=True, timeout=_PYTEST_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(
            f"test_plugin.py timed out after {_PYTEST_TIMEOUT_SEC}s "
            f"({test_plugin_path})") from exc
    return {"returncode": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr}


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


def submit_plugin(conn: sqlite3.Connection, meta: PluginMeta, *,
                  settings: "Settings", now: datetime,
                  pytest_runner: PytestRunnerFn | None = None,
                  sandbox_run: SandboxRunFn | None = None,
                  run_in_sample_fn: RunInSampleFn | None = None) -> int:
    """plugin (kind=plugin) の承認申請行を作り、その id を返す。

    どの検証段階が失敗しても `approval_requests` 行は作らない (fail
    closed)。`SandboxError` は全段階で `ValueError` に変換して送出する
    (承認フローの consumer としての統一契約)。
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
        check_source(meta.path / "plugin.py")
        check_source(test_plugin_path, extra_allowed=frozenset({"pytest", "plugin"}))

        runner = pytest_runner if pytest_runner is not None else _default_pytest_runner
        pytest_result = runner(test_plugin_path)
        if pytest_result["returncode"] != 0:
            raise ValueError(
                f"plugin {meta.name!r}: test_plugin.py failed pytest "
                f"(returncode={pytest_result['returncode']}): "
                f"{_pytest_summary(pytest_result.get('stdout', ''))}")

        metrics, evaluable = _validate_kind(
            conn, meta, settings=settings, now=now, sandbox_run=sandbox_run,
            run_in_sample_fn=run_in_sample_fn)
    except SandboxError as exc:
        raise ValueError(f"plugin {meta.name!r}: {exc}") from exc

    payload = {
        "name": meta.name,
        "kind": meta.kind,
        "content_hash": meta.content_hash,
        "test_file_hash": test_file_hash,
        "pytest": {"returncode": pytest_result["returncode"],
                   "summary": _pytest_summary(pytest_result.get("stdout", ""))},
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
    """`submit_plugin` と同一の検証を実行し、成功したら即座に承認する
    (D3)。検証失敗時は `submit_plugin` の例外がそのまま伝播し、
    `approval_requests` 行は作らない (`decide` にも到達しない)。

    CLI (`afx plugin bless`) からのみ呼ぶこと — 改善ループの tool 定義に
    は絶対に載せない。
    """
    approval_id = submit_plugin(
        conn, meta, settings=settings, now=now, pytest_runner=pytest_runner,
        sandbox_run=sandbox_run, run_in_sample_fn=run_in_sample_fn)
    approvals_store.decide(conn, approval_id, status="approved",
                           decided_by="human_cli", now=now)
    return approval_id
