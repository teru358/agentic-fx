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

from agentic_fx.tools.registry import ToolDef

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ALLOWED_REL = frozenset({"plugin.py", "config.yaml", "test_plugin.py"})


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


def build_improve_staging_tooldefs(*, staging_dir: Path,
                                    source_snapshot_dir: Path) -> list[ToolDef]:
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
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
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
        # 実機 E2E 是正 (2026-08-30 mission #9): (a) rootdir/confcutdir を
        # 候補 dir に固定し cwd も候補 dir にする — 未指定だと pytest の
        # rootdir 探索が Landlock allowlist 外 (リポジトリ root の
        # pyproject.toml 等) へ遡り PermissionError で収集前に死ぬ
        # (メモリ pytest-under-landlock-pitfalls)。(b) stderr も tail に
        # 含める — 収集前の死因は stderr にしか出ず、旧実装は
        # `stdout_tail:""` の盲目デバッグをモデルに強いていた。
        # `-c` 未指定では `locate_config()` が祖先の pyproject.toml を開き、
        # Landlock 下で EACCES になる (mission m20/m22 で実測)。
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:logging",
             "-p", "no:cacheprovider", "-c", "/dev/null",
             "--rootdir", str(base), "--confcutdir", str(base),
             str(base / "test_plugin.py")],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
            cwd=str(base), timeout=120)
        combined = result.stdout + (
            ("\n[stderr]\n" + result.stderr) if result.stderr else "")
        return {"passed": result.returncode == 0,
                "stdout_tail": combined[-2000:]}

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
