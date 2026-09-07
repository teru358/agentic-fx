"""improve registry の候補置き場ツール (設計書 §3.4/§2.3)。

根は `staging_dir` / `source_snapshot_dir` の 2 値だけから導く — Landlock の
rw/ro 境界と同じ根を prompt 側にも渡す (§2.2)。**ここでのパス正規化は
LocalRunner (worker 内 in-process) 用の防御** — claude/codex はネイティブ
ファイル操作でも Landlock により同じ場所しか書けない (§3.4 の注記)。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from agentic_fx.plugin import loader as plugin_loader
from agentic_fx.tools.registry import ToolDef

if TYPE_CHECKING:
    from agentic_fx.config import ImproveToolBudgetSettings
    from agentic_fx.tools.mission_counters import MissionToolCounters

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ALLOWED_REL = frozenset({"plugin.py", "config.yaml", "test_plugin.py"})

BUDGET_EXHAUSTED_DIRECTIVE = (
    "予算を使い切りました。現状の候補で提出するか、observation で理由を残して最終出力を出してください。")
RUN_BACKTEST_FIRST_DIRECTIVE = (
    "strategy 候補は self-test を繰り返す前に run_backtest(name, pair) で実データの挙動を確認してください。"
    "self-test の合否は run_backtest の前提ではありません。実データで trade が出れば plugin は正しく、"
    "失敗しているのはテスト側です。")
REPEATED_FAILURE_DIRECTIVE = (
    "同じテストが同じ assert で 3 回続けて失敗しています。テストデータの前提を疑ってください "
    "(evaluate は渡された df の最終バーで判定します。クロス等のイベントは最終バーで起きるデータにすること)。"
    "直らなければこのテストを外し、observation に理由を残して提出してください。")


def _failure_signature(full_output: str) -> tuple:
    failed = tuple(sorted(set(re.findall(r"^FAILED\s+(\S+)", full_output, re.MULTILINE))))
    if not failed:
        return ("<no-tests>", "", "")
    lines = full_output.splitlines()
    starts = [i for i, line in enumerate(lines)
              if re.match(r"^_{3,}\s+.+?\s+_{3,}$", line)]
    blocks = [lines[start:(starts[pos + 1] if pos + 1 < len(starts) else len(lines))]
              for pos, start in enumerate(starts)] or [lines]
    exc_types = []
    for block in blocks:
        for line in block:
            match = re.match(r"^E\s+([A-Za-z_][A-Za-z0-9_.]*):", line)
            if match:
                exc_types.append(match.group(1).rsplit(".", 1)[-1])
                break
    assertions = tuple(line.strip() for line in full_output.splitlines()
                       if line.lstrip().startswith(">"))
    return (failed, tuple(exc_types), assertions)


def _safe_join(root: Path, name: str, rel: str | None = None) -> Path | None:
    # round2 M1 是正 (2026-08-29、verified-round2.md M1): fullmatch に揃える
    # (`.match()` + `$` は末尾改行を受理する — probe 実測)。
    if not _NAME_RE.fullmatch(name):
        return None
    if rel is not None and rel not in _ALLOWED_REL:
        return None
    candidate = (root / name / rel) if rel is not None else (root / name)
    try:
        resolved_root = root.resolve()
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved_root not in (resolved, *resolved.parents):
        return None
    return candidate


def build_improve_staging_tooldefs(
        *, staging_dir: Path, source_snapshot_dir: Path,
        counters: "MissionToolCounters | None" = None,
        budget: "ImproveToolBudgetSettings | None" = None) -> list[ToolDef]:
    def list_staging() -> dict:
        # opencode E2E m11 実測 (2026-08-30): `_snapshot_src/` (staging_dir
        # 直下に実体化される snapshot) を候補として返すと、モデルは
        # read_staging_file で読もうとして _NAME_RE (先頭 `_` 不可) に必ず
        # 弾かれる。ツールで読めない名前は列挙しない。
        candidates = []
        for d in sorted(p for p in staging_dir.iterdir()
                        if p.is_dir() and _NAME_RE.fullmatch(p.name)):
            files = sorted(f.name for f in d.iterdir() if f.is_file())
            candidates.append({"name": d.name, "files": files})
        return {"candidates": candidates}

    def read_staging_file(name: str, rel: str) -> dict:
        path = _safe_join(staging_dir, name, rel)
        if path is None or not path.is_file():
            return {
                "error": "not found",
                "hint": "staging に無い名前/rel です。list_staging で確認し、"
                        "write_staging_file で先に作成してください",
            }
        return {"content": path.read_text(encoding="utf-8")}

    def write_staging_file(name: str, rel: str, content: str) -> dict:
        path = _safe_join(staging_dir, name, rel)
        if path is None:
            return {"error": "invalid name or rel"}
        if counters is not None and budget is not None \
                and not counters.reserve_write(budget.max_writes):
            return {"error": "budget exhausted", "budget": "max_writes",
                    "directive": BUDGET_EXHAUSTED_DIRECTIVE}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        if counters is not None and budget is None:
            counters.record_write()
        return {"ok": True}

    def read_plugin_source(name: str) -> dict:
        base = _safe_join(source_snapshot_dir, name)
        if base is None or not base.is_dir():
            return {
                "error": "not found",
                "hint": "deployed plugin が無い名前です。サンプルは "
                        "list_examples / read_example_plugin、作業中の候補は "
                        "list_staging / read_staging_file",
            }
        out = {}
        for rel in sorted(_ALLOWED_REL):
            p = base / rel
            if p.is_file():
                out[rel] = p.read_text(encoding="utf-8")
        if not out:
            return {
                "error": "not found",
                "hint": "deployed plugin が無い名前です。サンプルは "
                        "list_examples / read_example_plugin、作業中の候補は "
                        "list_staging / read_staging_file",
            }
        return out

    def list_examples() -> dict:
        examples_dir = source_snapshot_dir / "_examples"
        if not examples_dir.is_dir():
            return {"examples": []}
        examples = sorted(
            d.name for d in examples_dir.iterdir()
            if d.is_dir() and _NAME_RE.fullmatch(d.name))
        return {"examples": examples}

    def read_example_plugin(name: str) -> dict:
        examples_dir = source_snapshot_dir / "_examples"
        base = _safe_join(examples_dir, name)
        if base is not None and base.is_dir():
            out = {}
            for rel in sorted(_ALLOWED_REL):
                p = base / rel
                if p.is_file():
                    out[rel] = p.read_text(encoding="utf-8")
            if out:
                return out
        return {"error": "not found", "available": list_examples()["examples"]}

    def run_plugin_tests(name: str) -> dict:
        base = _safe_join(staging_dir, name)
        if base is None or not (base / "test_plugin.py").is_file():
            return {"error": "not found"}
        is_strategy = False
        if counters is not None and budget is not None:
            meta, _ = plugin_loader.discover_one_with_reason(base, name)
            is_strategy = meta is not None and meta.kind == "strategy"
            rejection = counters.reserve_self_test(
                name, max_runs=budget.max_self_test_runs,
                max_before_backtest=budget.max_self_tests_before_backtest,
                is_strategy=is_strategy)
            if rejection == "max_self_test_runs":
                return {"error": "budget exhausted", "budget": "max_self_test_runs",
                        "directive": BUDGET_EXHAUSTED_DIRECTIVE}
            if rejection == "run_backtest_required_first":
                return {"error": "run_backtest_required_first",
                        "directive": RUN_BACKTEST_FIRST_DIRECTIVE}
        # 実機 E2E 是正 (2026-08-30 mission #9): (a) rootdir/confcutdir を
        # 候補 dir に固定し cwd も候補 dir にする — 未指定だと pytest の
        # rootdir 探索が Landlock allowlist 外 (リポジトリ root の
        # pyproject.toml 等) へ遡り PermissionError で収集前に死ぬ
        # (メモリ pytest-under-landlock-pitfalls)。(b) stderr も tail に
        # 含める — 収集前の死因は stderr にしか出ず、旧実装は
        # `stdout_tail:""` の盲目デバッグをモデルに強いていた。
        # `-c` 未指定では `locate_config()` が祖先の pyproject.toml を開き、
        # Landlock 下で EACCES になる (mission m20/m22 で実測)。
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:logging",
                 "-p", "no:cacheprovider", "-c", "/dev/null",
                 "--rootdir", str(base), "--confcutdir", str(base),
                 str(base / "test_plugin.py")],
                capture_output=True, text=True, stdin=subprocess.DEVNULL,
                cwd=str(base), timeout=120)
        except subprocess.TimeoutExpired:
            if counters is not None:
                consecutive = counters.record_self_test_result(
                    name, ("<no-tests>", "", "")) if budget is not None else \
                    counters.record_self_test(name, ("<no-tests>", "", ""))
            else:
                consecutive = 0
            out = {"passed": False, "stdout_tail": "pytest timeout (120s)"}
            if budget is not None and consecutive >= budget.self_test_warn_after:
                out["repeated_failure"] = {
                    "consecutive": consecutive, "failed_tests": ["<no-tests>"],
                    "last_values": [], "directive": REPEATED_FAILURE_DIRECTIVE}
            return out
        combined = result.stdout + (
            ("\n[stderr]\n" + result.stderr) if result.stderr else "")
        out = {"passed": result.returncode == 0, "stdout_tail": combined[-2000:]}
        if counters is not None:
            signature = _failure_signature(combined)
            consecutive = counters.record_self_test_result(name, signature) \
                if budget is not None else counters.record_self_test(name, signature)
            if not out["passed"] and budget is not None \
                    and consecutive >= budget.self_test_warn_after:
                failed = [node.rsplit("::", 1)[-1]
                          for node in _failure_signature(combined)[0]]
                values = []
                for line in combined.splitlines():
                    match = re.match(r"^E\s+(?:AssertionError:\s*)?(assert\s.+)$", line)
                    if match:
                        values.append(match.group(1))
                    if len(values) == 5:
                        break
                out["repeated_failure"] = {
                    "consecutive": consecutive, "failed_tests": failed,
                    "last_values": values, "directive": REPEATED_FAILURE_DIRECTIVE}
        return out

    return [
        ToolDef(name="list_staging", description="候補置き場の一覧。",
                parameters={"type": "object", "properties": {}},
                func=list_staging),
        ToolDef(name="read_staging_file", description="候補ファイルを読む。",
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"},
                                          "rel": {"type": "string",
                                                  "enum": sorted(_ALLOWED_REL)}},
                            "required": ["name", "rel"]},
                func=read_staging_file),
        ToolDef(name="write_staging_file", description="候補ファイルを書く。",
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"},
                                          "rel": {"type": "string",
                                                  "enum": sorted(_ALLOWED_REL)},
                                          "content": {"type": "string"}},
                            "required": ["name", "rel", "content"]},
                func=write_staging_file),
        ToolDef(name="read_plugin_source",
                description="承認済み (deployed) plugin の読取専用スナップショットを読む。サンプルは read_example_plugin。",
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"]},
                func=read_plugin_source),
        ToolDef(name="list_examples", description="サンプル plugin (docs/examples) の一覧。",
                parameters={"type": "object", "properties": {}},
                func=list_examples),
        ToolDef(name="read_example_plugin",
                description="サンプル plugin (docs/examples) を読む。config.yaml の許可キーはここで確認する。",
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"]},
                func=read_example_plugin),
        ToolDef(name="run_plugin_tests", description="候補の test_plugin.py を回す (参考結果)。",
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"]},
                func=run_plugin_tests),
    ]
