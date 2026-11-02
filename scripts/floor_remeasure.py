#!/usr/bin/env python3
"""承認済み strategy などを現行条件で in-sample だけ新規に再生し、取引頻度の根拠表を作る。

plugin と履歴 DB は読み取り専用で開く。既定は dry-run で、DB へは何も書かない
(メトリクスを結果ファイルに追記するだけ)。`--write` を付けたときだけ、新規の
in-sample 行を `human_custom` として保存する。holdout は回さない。

使い方 (1 本ずつ、daemon が動いているので nice を付ける):

    nice -n 10 uv run python scripts/floor_remeasure.py list --root <実 repo>
    nice -n 10 uv run python scripts/floor_remeasure.py run --root <実 repo> \
        --only approval-21 --results tmp/results.jsonl
    uv run python scripts/floor_remeasure.py render --root <実 repo> \
        --results tmp/results.jsonl --out tmp/manifest.md
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agentic_fx.backtest.metrics import EVALUABLE_MIN_TRADES
from agentic_fx.plugin.strategy_gate import _floor_fail_items, trades_per_eval_bar

RECENT_DAYS = 60
REJECTED_REFERENCE_APPROVAL = 84


@dataclass(frozen=True)
class Target:
    label: str
    name: str
    content_hash: str
    mission_id: int | None
    artifact_hash: str | None = None
    approval_id: int | None = None
    source_row_id: int | None = None
    role: str = "approved"


# ---- 対象の抽出 (純粋部分) -------------------------------------------------

def _payload(raw: object) -> dict | None:
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _target_from_approval(row, *, role: str) -> Target | None:
    payload = _payload(row["payload_json"])
    if payload is None or payload.get("kind") != "strategy":
        return None
    name, content_hash = payload.get("name"), payload.get("content_hash")
    if not isinstance(name, str) or not isinstance(content_hash, str):
        return None
    artifact_hash = payload.get("artifact_hash")
    mission_id = payload.get("mission_id")
    return Target(
        label=f"approval-{row['id']}", name=name, content_hash=content_hash,
        mission_id=mission_id if type(mission_id) is int else None,
        artifact_hash=artifact_hash if isinstance(artifact_hash, str) else None,
        approval_id=row["id"], role=role)


def select_approved_targets(conn, *, rejected_ids=(REJECTED_REFERENCE_APPROVAL,)
                            ) -> list[Target]:
    """承認済みの strategy と、比較対象として明示した却下済みの承認を id 順に返す。"""
    targets: list[Target] = []
    for row in conn.execute(
            "SELECT id, status, payload_json FROM approval_requests "
            "WHERE kind='plugin' ORDER BY id"):
        if row["status"] == "approved":
            target = _target_from_approval(row, role="approved")
        elif row["id"] in rejected_ids:
            target = _target_from_approval(row, role="rejected_reference")
        else:
            continue
        if target is not None:
            targets.append(target)
    return targets


def _usable_metrics(raw: object) -> dict | None:
    """床の判定に要る最小のキーが揃った dict だけを返す。それ以外は None。"""
    try:
        m = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(m, dict) or not {"trades", "pf", "avg_r"} <= set(m):
        return None
    trades = m["trades"]
    if type(trades) is not int or trades < 0:
        return None
    for key in ("pf", "avg_r"):
        value = m[key]
        if value is None:
            continue
        if type(value) not in (int, float) or not math.isfinite(value):
            return None
    return m


def _is_deep_drawdown_candidate(per_pair: dict, *, settings) -> bool:
    """DD の上限だけ超え、他の床 (pf / avg_r / 取引頻度など) は通る。判定は実際の床と同じ関数。"""
    trades = [m["trades"] for m in per_pair.values()]
    if any(type(t) is not int for t in trades) or sum(trades) < EVALUABLE_MIN_TRADES:
        return False
    kinds = {item.kind for item in _floor_fail_items(
        per_pair, settings=settings, scope="in_sample")}
    return kinds == {"drawdown_too_deep"}


def select_deep_drawdown_candidate(
        conn, *, now: datetime, exclude_hashes, settings,
        window_days: int = RECENT_DAYS,
        skipped: list[int] | None = None) -> Target | None:
    """直近 window_days の candidate in-sample 行から、pf / avg_r / 取引数の床は
    通るが max DD が床の上限を超える候補 (content_hash 単位) のうち最新の 1 本。
    読めない行は行ごとに飛ばし、その id を `skipped` に足す。"""
    since = (now - timedelta(days=window_days)).isoformat()
    groups: dict[str, list] = {}
    for row in conn.execute(
            "SELECT id, plugin_ref, content_hash, pair, mission_id, metrics_json "
            "FROM backtest_runs WHERE scope='in_sample' AND issued_by='harness' "
            "AND variant='candidate' AND created_at >= ? ORDER BY id", (since,)):
        groups.setdefault(row["content_hash"], []).append(row)
    best: tuple[int, Target] | None = None
    for content_hash, rows in groups.items():
        if content_hash in exclude_hashes:
            continue
        per_pair: dict[str, dict] = {}
        usable = []
        for r in rows:
            m = _usable_metrics(r["metrics_json"])
            if m is None:
                if skipped is not None:
                    skipped.append(r["id"])
                continue
            per_pair[r["pair"]] = m
            usable.append(r)
        if not usable or not _is_deep_drawdown_candidate(per_pair, settings=settings):
            continue
        last = max(usable, key=lambda r: r["id"])
        ref = last["plugin_ref"]
        name = ref.rstrip("/").rsplit("/", 1)[-1]
        target = Target(
            label=f"run-{last['id']}", name=name, content_hash=content_hash,
            mission_id=last["mission_id"], source_row_id=last["id"],
            role="deep_drawdown")
        if best is None or last["id"] > best[0]:
            best = (last["id"], target)
    return best[1] if best else None


def locate_artifact(plugins_root: Path, target: Target) -> tuple[Path, str] | None:
    """immutable な artifact の場所を (path, 種別) で返す。無ければ None。

    artifact_hash が分かるときは版ストア、次に archive の同名ディレクトリを見る。
    分からないときは archive の meta.json の content_hash で探す。"""
    if target.artifact_hash:
        versions = plugins_root / ".versions" / target.name / target.artifact_hash
        if versions.is_dir():
            return versions, "versions"
        if target.mission_id is not None:
            archive = plugins_root / "_archive" / str(target.mission_id) / target.artifact_hash
            if archive.is_dir():
                return archive, "archive"
        return None
    if target.mission_id is None:
        return None
    base = plugins_root / "_archive" / str(target.mission_id)
    if not base.is_dir():
        return None
    for child in sorted(base.iterdir()):
        meta_file = child / "meta.json"
        if not meta_file.is_file():
            continue
        meta = _payload(meta_file.read_text(encoding="utf-8"))
        if meta and meta.get("content_hash") == target.content_hash:
            return child, "archive"
    return None


def reference_row(conn, content_hash: str, pair: str) -> dict | None:
    """元の in-sample 行 (参考列)。並べるだけで、一致は求めない。"""
    row = conn.execute(
        "SELECT id, created_at, metrics_json FROM backtest_runs "
        "WHERE scope='in_sample' AND issued_by='harness' AND variant='candidate' "
        "AND content_hash=? AND pair=? ORDER BY id DESC LIMIT 1",
        (content_hash, pair)).fetchone()
    if row is None:
        return None
    metrics = _payload(row["metrics_json"]) or {}
    return {"id": row["id"], "created_at": row["created_at"],
            "trades": metrics.get("trades"),
            "max_drawdown": metrics.get("max_drawdown")}


# ---- manifest 整形 (純粋部分) ----------------------------------------------

def _num(value: object, digits: int = 3) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _short(value: object, n: int = 12) -> str:
    return str(value)[:n] if value else "-"


def _row_cells(cells: list[object]) -> str:
    return "| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |"


def _table(header: list[str], rows: list[list[object]]) -> list[str]:
    lines = [_row_cells(header), "|" + "---|" * len(header)]
    lines.extend(_row_cells(r) for r in rows)
    return lines


def _results_by_pair(results: list[dict]) -> list[tuple[int, dict, dict]]:
    return [(number, item, run) for number, item in enumerate(results, 1)
            if not item.get("exclusion") or item.get("partial_saved")
            for run in item["runs"]]


def frequency_summary(results: list[dict]) -> dict:
    """承認済みの最大、却下済みの比較対象、床通過 DD 超過候補の頻度。"""
    summary: dict[str, list[float]] = {"approved": [], "rejected_reference": [],
                                       "deep_drawdown": []}
    for _n, item, run in _results_by_pair(results):
        if item.get("exclusion"):
            continue
        freq = run.get("frequency")
        if freq is not None:
            summary.setdefault(item["role"], []).append(freq)
    return {role: (max(values) if values else None)
            for role, values in summary.items()}


def render_manifest(results: list[dict], *, header: dict) -> str:
    """results は run_target の戻り値 (JSON 化可能 dict) の並び。"""
    lines = [
        "# 床拡張 取引頻度の再実測 manifest",
        "",
        "現行の runner・settings・履歴で in-sample だけを新規に再生した値。"
        "元行の再現ではない。holdout の値・期間は含まない。",
        "",
        f"- 保存: {header.get('mode', 'dry-run')}",
        "",
        "## 識別",
        "",
    ]
    lines += _table(
        ["#", "役割", "戦略", "content_hash", "artifact", "artifact_hash", "承認 # / 元 row id"],
        [[i + 1, item["role"], item["name"], _short(item["content_hash"]),
          f"{item.get('artifact_kind') or '-'}: {item.get('artifact_path') or '-'}",
          _short(item.get("artifact_hash")),
          (f"#{item['approval_id']}" if item.get("approval_id") is not None
           else f"row {item.get('source_row_id')}")]
         for i, item in enumerate(results)])
    lines += ["", "## 新規実行の条件 (in-sample)", ""]
    lines += _table(
        ["#", "pair", "timeframe", "source", "base_interval", "run id", "now",
         "in-sample start", "in-sample end", "settings_hash", "core_commit"],
        [[number, run["pair"], run["timeframe"], run["source"],
          run["base_interval"], run.get("run_id") or "dry-run (未保存)", run["now"],
          run["start"], run["end"], _short(run["settings_hash"]),
          _short(run["core_commit"], 10)]
         for number, item, run in _results_by_pair(results)])
    lines += ["", "## 結果 (新規実行) と参考列 (元行)", ""]
    lines += _table(
        ["#", "pair", "eval_bars", "trades", "頻度 (trades/eval_bars)", "DD",
         "元 trades", "元 DD", "元 created_at", "元 row id"],
        [[number, run["pair"], run["eval_bars"], run["trades"],
          _num(run.get("frequency")), _num(run["max_drawdown"]),
          _num((run.get("reference") or {}).get("trades")),
          _num((run.get("reference") or {}).get("max_drawdown")),
          (run.get("reference") or {}).get("created_at") or "-",
          (run.get("reference") or {}).get("id") or "-"]
         for number, item, run in _results_by_pair(results)])
    excluded = [(number, item) for number, item in enumerate(results, 1)
                if item.get("exclusion")]
    lines += ["", "## 除外", ""]
    if excluded:
        lines += _table(["#", "戦略", "content_hash", "理由"],
                        [[number, item["name"], _short(item["content_hash"]),
                          item["exclusion"]]
                         for number, item in excluded])
    else:
        lines.append("なし")
    summary = frequency_summary(results)
    lines += ["", "## 頻度の要約 (除外を除く)", ""]
    lines += _table(["区分", "最大頻度"],
                    [["承認済み", _num(summary.get("approved"))],
                     ["却下済み比較対象", _num(summary.get("rejected_reference"))],
                     ["床通過 DD 超過", _num(summary.get("deep_drawdown"))]])
    return "\n".join(lines) + "\n"


# ---- 実行 (impure) ---------------------------------------------------------

def run_target(target: Target, *, root: Path, settings, ro_conn, write_conn,
               now: datetime) -> dict:
    """1 本を pair ごとに in-sample で再生して結果 dict を返す。例外は除外理由にする。"""
    from agentic_fx.backtest import holdout
    from agentic_fx.plugin import loader as plugin_loader
    from agentic_fx.plugin import sandbox as plugin_sandbox
    from agentic_fx.plugin import strategy_adapter
    from agentic_fx.plugin.resolve import IndicatorResolutionError, resolve_indicator_deps
    from agentic_fx.plugin.strategy_gate import _eval_timeframe
    from agentic_fx.store import backtest_runs
    from agentic_fx.tools import plugin_loader as tools_plugin_loader

    record = {**asdict(target), "runs": [], "exclusion": None,
              "partial_saved": False,
              "artifact_path": None, "artifact_kind": None}

    def excluded(reason: str) -> dict:
        record["exclusion"] = reason
        return record

    plugins_dir = root / "plugins"
    located = locate_artifact(plugins_dir, target)
    if located is None:
        return excluded("artifact 欠落 (版ストア / archive に見つからない)")
    path, kind = located
    record["artifact_path"], record["artifact_kind"] = str(path), kind
    meta, reason = plugin_loader.discover_one_with_reason(path, target.name)
    if meta is None:
        return excluded(f"artifact を読めない: {reason}")
    if meta.content_hash != target.content_hash:
        return excluded("content_hash 不一致 (artifact が承認時と異なる)")
    if target.artifact_hash and meta.artifact_hash != target.artifact_hash:
        return excluded("artifact_hash 不一致")
    record["artifact_hash"] = meta.artifact_hash
    if meta.kind != "strategy":
        return excluded(f"kind={meta.kind}")
    dataset = settings.backtest.dataset()
    if dataset.unconfigured:
        return excluded("履歴源が未設定")
    try:
        plugin_sandbox.check_source(meta.path / "plugin.py")
    except plugin_sandbox.SandboxError as exc:
        return excluded(f"sandbox 検査を通らない: {type(exc).__name__}")
    inventory = tools_plugin_loader.approved_plugins(
        ro_conn, plugins_dir, settings=settings)
    try:
        resolved = resolve_indicator_deps(
            meta, inventory.inventory, settings=settings, pin_mode="check")
    except IndicatorResolutionError as exc:
        return excluded(str(exc))
    try:
        holdout.require_reference_history(ro_conn, dataset, resolved)
    except holdout.ReferenceHistoryMissingError as exc:
        return excluded(f"参照履歴の不足: {exc}")
    timeframe = _eval_timeframe(meta.timeframe)
    for pair in meta.pairs:
        captured: list[dict] = []
        saved_ids: list[int] = []

        def sink(kwargs: dict, _captured=captured, _saved=saved_ids) -> int:
            _captured.append(kwargs)
            if write_conn is None:
                return 0
            saved = {k: v for k, v in kwargs.items() if k != "scope"}
            run_id = backtest_runs.save_human_run(write_conn, **saved)
            _saved.append(run_id)
            return run_id

        source = strategy_adapter.build_intent_source(
            meta, conn=ro_conn, pair=pair, dataset=dataset,
            settings=settings, resolved=resolved)
        try:
            holdout.run_in_sample(
                settings, history_conn=ro_conn, symbol=pair, dataset=dataset,
                intent_source=source, eval_timeframe=timeframe,
                plugin_ref=f"plugins/{meta.name}", content_hash=meta.content_hash,
                kind="strategy", now=now, record_fn=sink)
        except Exception as exc:
            source.close()
            reason = f"{pair}: 再生に失敗 ({type(exc).__name__}: {str(exc)[:200]})"
            if write_conn is not None and record["runs"]:
                record["partial_saved"] = True
                reason = (f"部分保存 (保存済み {len(record['runs'])} pair は下表に"
                          f"載せた。DB に残っている): {reason}")
            if saved_ids:
                partial = f"{pair} の行 id={saved_ids[-1]} も保存済み"
                record["partial_saved"] = True
                reason = f"部分保存 ({partial}): {reason}" \
                    if not reason.startswith("部分保存") else f"{reason} / {partial}"
            return excluded(reason)
        source.close()
        kwargs = captured[0]
        metrics = kwargs["metrics"]
        start, end = kwargs["period"]
        record["runs"].append({
            "pair": pair, "timeframe": timeframe, "source": kwargs["source"],
            "base_interval": kwargs["base_interval"],
            "run_id": saved_ids[-1] if saved_ids else None,
            "now": kwargs["now"].isoformat(), "start": start.isoformat(),
            "end": end.isoformat(), "settings_hash": kwargs["settings_hash"],
            "core_commit": kwargs["core_commit"],
            "eval_bars": metrics.get("eval_bars"), "trades": metrics["trades"],
            "frequency": trades_per_eval_bar(metrics["trades"], metrics.get("eval_bars")),
            "max_drawdown": metrics["max_drawdown"], "pf": metrics["pf"],
            "avg_r": metrics["avg_r"],
            "reference": reference_row(ro_conn, target.content_hash, pair)})
    return record


def build_targets(ro_conn, *, now: datetime, settings) -> list[Target]:
    approved = select_approved_targets(ro_conn)
    skipped: list[int] = []
    deep = select_deep_drawdown_candidate(
        ro_conn, now=now, exclude_hashes={t.content_hash for t in approved},
        settings=settings, skipped=skipped)
    if skipped:
        print(f"読めない backtest_runs 行を {len(skipped)} 件飛ばしました",
              file=sys.stderr)
    return approved + ([deep] if deep is not None else [])


def _open(root: Path, db: Path | None):
    from agentic_fx.store import db as store_db
    return store_db.connect_observer_readonly(db or root / "data" / "agentic.db")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("list", "run", "render"))
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="plugins/ data/ config/ を持つ repo (読み取り専用)")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--settings", type=Path, default=None)
    parser.add_argument("--only", help="対象の label (例: approval-21)。run で必須")
    parser.add_argument("--results", type=Path, default=Path("tmp/floor-results.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("tmp/floor-manifest.md"))
    parser.add_argument("--write", action="store_true",
                        help="新規 in-sample 行を human_custom として保存する (既定は dry-run)")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    if args.command == "render":
        results = [json.loads(line) for line in
                   args.results.read_text(encoding="utf-8").splitlines() if line.strip()]
        latest = {item["label"]: item for item in results}
        ordered = [latest[label] for label in dict.fromkeys(i["label"] for i in results)]
        args.out.write_text(
            render_manifest(ordered, header={"mode": "write" if args.write else "dry-run"}),
            encoding="utf-8")
        print(f"{len(ordered)} 件 -> {args.out}")
        return 0

    from agentic_fx.config import load_settings
    settings = load_settings(args.settings or root / "config" / "settings.yaml")
    now = datetime.now(timezone.utc)
    ro_conn = _open(root, args.db)
    try:
        targets = build_targets(ro_conn, now=now, settings=settings)
        if args.command == "list":
            for t in targets:
                print(f"{t.label}\t{t.role}\t{t.name}\t{t.content_hash[:12]}\tmission={t.mission_id}")
            return 0
        chosen = [t for t in targets if t.label == args.only]
        if not chosen:
            print(f"対象が見つかりません: {args.only!r}", file=sys.stderr)
            return 2
        write_conn = None
        if args.write:
            from agentic_fx.store.db import connect_existing_rw
            write_conn = connect_existing_rw(args.db or root / "data" / "agentic.db")
        try:
            record = run_target(chosen[0], root=root, settings=settings,
                                ro_conn=ro_conn, write_conn=write_conn, now=now)
        finally:
            if write_conn is not None:
                write_conn.close()
        args.results.parent.mkdir(parents=True, exist_ok=True)
        with args.results.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(json.dumps(
            {"label": chosen[0].label, "exclusion": record["exclusion"],
             "runs": [{k: r[k] for k in ("pair", "eval_bars", "trades", "frequency",
                                         "max_drawdown")} for r in record["runs"]]},
            ensure_ascii=False))
        return 0
    finally:
        ro_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
