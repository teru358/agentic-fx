"""人間 CLI — history import/compare/coverage、backtest run、analyze corr
(プラン 6 Task 11)。

Task 4-10 で実装済みの importer / runner / metrics / analysis 関数を配線
するだけの薄いアダプタ。発注系ロジックはここに書かない。

DB 解決契約 (レビュー裁定 codex M5): root は ``Path.cwd()`` (entry.py が渡す)、
DB は ``root / "data" / "agentic.db"`` (init 済みを要求 — 無ければ
``ensure_initialized`` が案内して ``SystemExit(2)``)。CLI が触るのは
この実 DB のみ — in-memory 再生 DB は ``run_replay`` 内部に閉じる。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agentic_fx.backtest.analysis import coverage_report, corr_matrix
from agentic_fx.backtest.importer import import_dukascopy
from agentic_fx.backtest.metrics import compute_metrics
from agentic_fx.backtest.mt5_import import compare_sources, import_mt5
from agentic_fx.backtest.runner import run_replay
from agentic_fx.config import load_settings
from agentic_fx.core.contracts import Bar, Origin, TradeIntent
from agentic_fx.service import ensure_initialized
from agentic_fx.store import backtest_runs
from agentic_fx.store.db import connect, init_db

# analyze corr で --to 未指定時の in_sample_until (aware far-future 定数 —
# 上書き節 D 逐語)。人間の探索は期間自由なので実質「上限なし」を表す。
_NO_LIMIT = datetime(9999, 12, 28, tzinfo=timezone.utc)


def _parse_date(v: str) -> datetime:
    """``YYYY-MM-DD`` (時刻付き ISO も可) を aware UTC に変換する。

    時刻付き ISO でも tzinfo は無視し (naive として解釈)、UTC を付与する
    (上書き節 B 逐語)。
    """
    return datetime.fromisoformat(v).replace(tzinfo=timezone.utc)


def register_subparsers(sub: "argparse._SubParsersAction") -> None:
    """entry.py の ``sub`` (top-level subparsers) に history/backtest/analyze
    を登録する。"""
    history = sub.add_parser("history", help="履歴データ操作")
    history_sub = history.add_subparsers(dest="history_command", required=True)

    imp = history_sub.add_parser("import", help="一括取り込み (進捗を件数で表示)")
    imp.add_argument("--source", choices=["dukascopy", "mt5"], required=True)
    imp.add_argument("--symbol", required=True)
    imp.add_argument("--from", dest="from_", type=_parse_date, required=True)
    imp.add_argument("--to", dest="to", type=_parse_date, required=True)

    comp = history_sub.add_parser("compare", help="source 間の close 差照合")
    comp.add_argument("--symbol", required=True)

    cov = history_sub.add_parser("coverage", help="カバレッジレポート")
    cov.add_argument("--symbol", required=True)
    cov.add_argument("--timeframe", required=True)
    cov.add_argument("--source", required=True)
    cov.add_argument("--from", dest="from_", type=_parse_date, required=True)
    cov.add_argument("--to", dest="to", type=_parse_date, required=True)

    backtest = sub.add_parser("backtest", help="バックテスト")
    backtest_sub = backtest.add_subparsers(dest="backtest_command",
                                           required=True)
    run = backtest_sub.add_parser(
        "run", help="人間用バックテスト実行 (自由期間・scope=human_custom)")
    run.add_argument("--symbol", required=True)
    run.add_argument("--source", required=True)
    run.add_argument("--from", dest="from_", type=_parse_date, required=True)
    run.add_argument("--to", dest="to", type=_parse_date, required=True)
    run.add_argument("--proposal-file", required=True)
    run.add_argument("--timeframe", default="1h")

    analyze = sub.add_parser("analyze", help="履歴分析")
    analyze_sub = analyze.add_subparsers(dest="analyze_command",
                                         required=True)
    corr = analyze_sub.add_parser("corr", help="2 ペアの相関 (人間の自由探索)")
    corr.add_argument("--a", required=True)
    corr.add_argument("--b", required=True)
    corr.add_argument("--timeframe", required=True)
    corr.add_argument("--source", required=True)
    corr.add_argument("--from", dest="from_", type=_parse_date, default=None)
    corr.add_argument("--to", dest="to", type=_parse_date, default=None)


# ---- history import ------------------------------------------------------


def _make_dukascopy_progress():
    """時間単位の進捗コールバック — 件数を間引いて表示する。"""
    state = {"n": 0}

    def _cb(_current: datetime) -> None:
        state["n"] += 1
        if state["n"] % 100 == 0:
            print(f"import: {state['n']} 時間分処理済み")

    return _cb


def _history_import(conn, settings, args: argparse.Namespace) -> int:
    if args.source == "dukascopy":
        result = import_dukascopy(conn, args.symbol, args.from_, args.to,
                                  progress=_make_dukascopy_progress())
    else:  # mt5
        bridge_url = settings.datafeed.mt5.bridge_url
        if bridge_url is None:
            print("datafeed.mt5.bridge_url が未設定です", file=sys.stderr)
            return 1
        result = import_mt5(conn, args.symbol, args.from_, args.to,
                            base_url=bridge_url)
    print(f"inserted={result.inserted} unchanged={result.unchanged} "
         f"conflicted={result.conflicted}")
    return 0


def _history_compare(conn, settings, args: argparse.Namespace) -> int:
    result = compare_sources(conn, args.symbol, settings)
    print(result)
    return 0


def _history_coverage(conn, args: argparse.Namespace) -> int:
    result = coverage_report(conn, args.symbol, timeframe=args.timeframe,
                             source=args.source, start=args.from_,
                             end=args.to)
    print(result)
    return 0


# ---- backtest run ----------------------------------------------------------


def _load_proposals(path: Path) -> list[tuple[datetime, dict]]:
    """proposal-file (JSONL) を全行 parse し、ts 昇順に整列して返す。

    ts が欠落・parse 不能・naive なら ``ValueError`` (fail closed、部分実行
    しない — 上書き節 F 逐語)。

    Fix Round 1 F3 (sonnet I-4 = codex Important): ts だけでなく intent 本体
    の形状も **副作用 (run_replay 実行) の前に全行** 検証する。検証は
    ``runner.py`` の実呼び出し (``TradeIntent.from_llm_dict(pending_proposal,
    origin=Origin.SCHEDULER)``) と同じ形で行い、1 行でも不正なら
    ``ValueError`` (fail closed、何も実行しない)。許容 action は ``open``
    のみ (brief 「OPEN_INTENT 形」の裁定 — hold/close/cancel は proposal-file
    としては不正)。
    """
    proposals: list[tuple[datetime, dict]] = []
    text = path.read_text(encoding="utf-8")
    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"line {lineno}: invalid JSON ({e})") from e
        if not isinstance(obj, dict) or "ts" not in obj:
            raise ValueError(f"line {lineno}: missing 'ts'")
        ts_raw = obj["ts"]
        try:
            ts = datetime.fromisoformat(ts_raw)
        except (TypeError, ValueError) as e:
            raise ValueError(f"line {lineno}: unparsable ts {ts_raw!r} ({e})") from e
        if ts.tzinfo is None:
            raise ValueError(f"line {lineno}: ts must be timezone-aware")
        ts = ts.astimezone(timezone.utc)
        intent = {k: v for k, v in obj.items() if k != "ts"}
        if intent.get("action") != "open":
            raise ValueError(
                f"line {lineno}: only action='open' is supported in "
                f"proposal-file, got {intent.get('action')!r}")
        # IntentParseError は ValueError のサブクラスなのでそのまま伝播させる
        # (ここで捕まえて包み直す必要はない — 呼び出し元は ValueError を見る)。
        TradeIntent.from_llm_dict(intent, origin=Origin.SCHEDULER)
        proposals.append((ts, intent))
    proposals.sort(key=lambda p: p[0])
    return proposals


class _ProposalIntentSource:
    """JSONL 提案列を ``IntentSource`` (Bar -> dict | None) に変換する薄い
    アダプタ (プラン 7 で ``--plugin`` に置き換わる — 上書き節 F)。

    ``closed_bar.ts >= ts`` の最古の未消費提案を 1 件返す。1 呼び出し
    (1 バー) で 1 件のみ消費し、残りは次バー以降に持ち越す。この「1 バー
    1 件」契約は ``run_replay`` が 1 closed bar につき本アダプタを最大 1 回
    しか呼ばない前提 (runner.py の評価バケット確定処理) に依存する —
    同一 ``Bar`` で 2 回呼ばれると 2 件消費してしまう (codex Minor 裁定)。
    """

    def __init__(self, proposals: list[tuple[datetime, dict]]) -> None:
        self._proposals = proposals
        self._idx = 0

    def __call__(self, closed_bar: Bar) -> dict | None:
        if self._idx >= len(self._proposals):
            return None
        ts, intent = self._proposals[self._idx]
        if closed_bar.ts >= ts:
            self._idx += 1
            return intent
        return None


def _backtest_run(conn, settings, args: argparse.Namespace) -> int:
    proposal_path = Path(args.proposal_file).resolve()
    try:
        proposals = _load_proposals(proposal_path)
    except ValueError as e:
        print(f"提案ファイルの読み込みに失敗しました: {e}", file=sys.stderr)
        return 1

    intent_source = _ProposalIntentSource(proposals)
    content_hash = hashlib.sha256(proposal_path.read_bytes()).hexdigest()

    # F4 (最終レビュー opus I-4): 対象 source/期間に 1m 履歴が 0 行なら
    # run_replay を呼ばずに fail closed する。run_replay 自体は空バー窓
    # (市場クローズ期間等) の再生を前提にした既存テストと衝突しないよう
    # 変更しない (裁定) — CLI 側で先に検査する。0 行のまま黙って完走すると
    # 正常系と見分けのつかない human_custom 行が残る (--source のタイプミス
    # 等を検出できない)。
    n_bars = conn.execute(
        "SELECT COUNT(*) FROM ohlcv WHERE symbol=? AND interval='1m' "
        "AND source=? AND bar_time >= ? AND bar_time < ?",
        (args.symbol, args.source, args.from_.isoformat(),
         args.to.isoformat())).fetchone()[0]
    if n_bars == 0:
        print(f"エラー: symbol={args.symbol} source={args.source} の指定期間に "
             "1m 履歴が 0 件です (source のタイプミスや未インポート期間の "
             "可能性があります)", file=sys.stderr)
        return 1

    result = run_replay(settings, symbol=args.symbol, source=args.source,
                        start=args.from_, end=args.to,
                        intent_source=intent_source,
                        eval_timeframe=args.timeframe, history_conn=conn)
    metrics = compute_metrics(result)
    run_id = backtest_runs.save_human_run(
        conn, plugin_ref=str(proposal_path), content_hash=content_hash,
        kind="proposals", pair=args.symbol, timeframe=args.timeframe,
        source=args.source, period=(args.from_, args.to), metrics=metrics,
        settings_hash=backtest_runs.settings_snapshot_hash(settings),
        core_commit=backtest_runs.core_commit(),
        initial_balance=settings.backtest.initial_balance,
        now=datetime.now(timezone.utc))
    print(f"backtest run id={run_id}")
    print(metrics)
    return 0


# ---- analyze corr -----------------------------------------------------------


def _analyze_corr(conn, args: argparse.Namespace) -> int:
    in_sample_until = args.to if args.to is not None else _NO_LIMIT
    result = corr_matrix(conn, [args.a, args.b], timeframe=args.timeframe,
                         source=args.source, in_sample_until=in_sample_until,
                         since=args.from_)
    print(result[(args.a, args.b)])
    return 0


# ---- dispatch ---------------------------------------------------------------


def dispatch(args: argparse.Namespace, root: Path) -> int:
    ensure_initialized(root)
    settings = load_settings(root / "config" / "settings.yaml")
    conn = connect(root / "data" / "agentic.db")
    init_db(conn)

    # Fix Round 1 F6 (sonnet I-3): 統一エラー境界。人間向け CLI なので生の
    # traceback を出さない — 診断メッセージを stderr に出して rc=1 とする。
    # SystemExit (ensure_initialized 由来) は Exception ではないのでここを
    # 経由せず素通りする。
    try:
        if args.command == "history":
            if args.history_command == "import":
                return _history_import(conn, settings, args)
            if args.history_command == "compare":
                return _history_compare(conn, settings, args)
            return _history_coverage(conn, args)
        if args.command == "backtest":
            return _backtest_run(conn, settings, args)
        return _analyze_corr(conn, args)
    except (ValueError, KeyError, OSError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        return 1
