"""improve registry の RPC ツール — run_backtest / analyze_corr
(設計書 §3.4)。親 RPC 経由 (WorkerRunner の tool_rpc フレーム) で実行される
handler をラップし、台帳への record と遮断 7 のキー剥がしだけを行う。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml

from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
from agentic_fx.tools.improve_staging_tools import _safe_join
from agentic_fx.tools.registry import ToolDef

_RUN_BACKTEST_KIND_HINT = (
    "indicator/signal plugin はバックテストできません "
    "(決済規則を持たないため、設計 §6)。self-test (run_plugin_tests) で検証し、"
    "そのまま最終出力へ進んでください。strategy を作る場合は config.yaml に "
    "kind: strategy / timeframe / pairs / exit_mode / max_bars を書きます "
    '(read_example_plugin(name="sma_cross") 参照)')

_FORBIDDEN_KEYS = frozenset({
    "period_start", "period_end", "start", "end", "window", "timestamps",
    # `period`/`now` (10.9 節 Step 11 追記): `_build_rpc_handlers` の
    # run_backtest handler は `_run_scope` の save_kwargs (`period=
    # (period_start, period_end)` タプル、`now` datetime) をそのまま
    # 戻り値へ混ぜて ledger.record の result_summary に載せる (10.10 節
    # `_persist_ledger_rows` が読むための契約)。この 2 キーを剥がさないと
    # `period_start`/`period_end` という平坦キーは無くても `period` タプル
    # 経由・`now` 経由で期間端点/日時が agent へ漏れ、遮断7 の趣旨
    # (「返却 schema に日時・期間端点…が無い」) に反する。
    "period", "now",
    # `in_sample_until` (Task 7 検収 B2 是正): `analyze_for_agent(persist=
    # False)` (`backtest/analysis.py`) は `save_params = {"params": {
    # "request": ..., "in_sample_until": <ISO 文字列>}, "trial_count": ...,
    # "source": ...}` をそのまま `payload_body` にマージして返す
    # (`persist=False` の分岐)。`params` はトップレベルの禁止キーではなく、
    # `in_sample_until` はその**入れ子**にしか現れないため、剥がすキーの
    # 集合を平坦に列挙するだけでは足りない — `_strip_forbidden` 自体を
    # 再帰化し、`in_sample_until` を禁止キーに加える。
    "in_sample_until"})


def _strip_forbidden(value: object) -> object:
    """§遮断7: 禁止キーを dict/list の任意の深さから剥がして投影する
    (Task 7 検収 B2 是正 — 元は浅い (トップレベルのみ) 実装で、
    `analyze_corr` (persist=False 経路) の `params.in_sample_until` を
    見落としていた)。

    **台帳は痩せない**: `build_improve_rpc_tooldefs` 内の `ledger.record`
    呼び出しは strip **前**の `result` をそのまま渡す (10.10 節
    `_persist_ledger_rows` の契約) — ここでの再帰化は agent への戻り値の
    投影にのみ影響し、台帳の記録内容は変えない。"""
    if isinstance(value, dict):
        return {k: _strip_forbidden(v) for k, v in value.items()
                if k not in _FORBIDDEN_KEYS}
    if isinstance(value, (list, tuple)):
        # L21: tuple 要素も list と同じく再帰する (型は保存する)。
        stripped = [_strip_forbidden(v) for v in value]
        return tuple(stripped) if isinstance(value, tuple) else stripped
    return value


def build_improve_rpc_tooldefs(
        *, ledger: ImproveRpcLedger,
        run_backtest_handler: Callable[[dict], dict],
        analyze_corr_handler: Callable[[dict], dict],
        staging_dir: Path | None = None) -> list[ToolDef]:
    def run_backtest(name: str, pair: str) -> dict:
        config_path = (_safe_join(staging_dir, name, "config.yaml")
                       if staging_dir is not None else None)
        if config_path is not None and config_path.is_file():
            try:
                config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
                candidate_kind = config.get("kind") if isinstance(config, dict) else None
            except (OSError, UnicodeError, yaml.YAMLError):
                candidate_kind = None
            if isinstance(candidate_kind, str) and candidate_kind != "strategy":
                return {
                    "error": "run_backtest is only for kind=strategy candidates",
                    "candidate_kind": candidate_kind,
                    "hint": _RUN_BACKTEST_KIND_HINT,
                }
        result = run_backtest_handler({"name": name, "pair": pair})
        ledger.record(opaque_ref=f"run_backtest:{name}:{pair}",
                      kind="run_backtest", params={"name": name, "pair": pair},
                      result_summary=result,
                      trial_count=result.get("trial_count", 1))
        return _strip_forbidden(result)

    def analyze_corr(request: dict) -> dict:
        result = analyze_corr_handler(request)
        ledger.record(opaque_ref=f"analyze_corr:{id(request)}",
                      kind="analyze_corr", params=request,
                      result_summary=result,
                      trial_count=result.get("trial_count", 1))
        return _strip_forbidden(result)

    return [
        ToolDef(name="run_backtest", description=(
                    "kind=strategy の候補専用。親が in-sample バックテストを回す。"),
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"},
                                          "pair": {"type": "string"}},
                            "required": ["name", "pair"]},
                func=run_backtest),
        ToolDef(name="analyze_corr", description="親が相関分析を回す。",
                parameters={"type": "object",
                            "properties": {"request": {"type": "object"}},
                            "required": ["request"]},
                func=analyze_corr),
    ]
