"""improve registry の候補置き場ツール (設計書 §3.4/§2.3)。

根は `staging_dir` / `source_snapshot_dir` の 2 値だけから導く — Landlock の
rw/ro 境界と同じ根を prompt 側にも渡す (§2.2)。**ここでのパス正規化は
LocalRunner (worker 内 in-process) 用の防御** — claude/codex はネイティブ
ファイル操作でも Landlock により同じ場所しか書けない (§3.4 の注記)。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from agentic_fx.tools.registry import ToolDef

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ALLOWED_REL = frozenset({"plugin.py", "config.yaml", "test_plugin.py"})


def _safe_join(root: Path, name: str, rel: str | None = None) -> Path | None:
    if not _NAME_RE.match(name):
        return None
    if rel is not None and rel not in _ALLOWED_REL:
        return None
    candidate = (root / name / rel) if rel is not None else (root / name)
    try:
        resolved_root = root.resolve()
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved_root not in resolved.parents and resolved != resolved_root:
        return None
    if resolved_root not in (resolved, *resolved.parents):
        return None
    return candidate


def build_improve_staging_tooldefs(*, staging_dir: Path,
                                    source_snapshot_dir: Path) -> list[ToolDef]:
    def list_staging() -> str:
        candidates = []
        for d in sorted(p for p in staging_dir.iterdir() if p.is_dir()):
            files = sorted(f.name for f in d.iterdir() if f.is_file())
            candidates.append({"name": d.name, "files": files})
        return json.dumps({"candidates": candidates})

    def read_staging_file(name: str, rel: str) -> str:
        path = _safe_join(staging_dir, name, rel)
        if path is None or not path.is_file():
            return json.dumps({"error": "not found"})
        return json.dumps({"content": path.read_text(encoding="utf-8")})

    def write_staging_file(name: str, rel: str, content: str) -> str:
        path = _safe_join(staging_dir, name, rel)
        if path is None:
            return json.dumps({"error": "invalid name or rel"})
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return json.dumps({"ok": True})

    def read_plugin_source(name: str) -> str:
        base = _safe_join(source_snapshot_dir, name)
        if base is None or not base.is_dir():
            return json.dumps({"error": "not found"})
        out = {}
        for rel in sorted(_ALLOWED_REL):
            p = base / rel
            if p.is_file():
                out[rel] = p.read_text(encoding="utf-8")
        if not out:
            return json.dumps({"error": "not found"})
        return json.dumps(out)

    def run_plugin_tests(name: str) -> str:
        base = _safe_join(staging_dir, name)
        if base is None or not (base / "test_plugin.py").is_file():
            return json.dumps({"error": "not found"})
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:logging",
             str(base / "test_plugin.py")],
            capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)
        return json.dumps({"passed": result.returncode == 0,
                           "stdout_tail": result.stdout[-2000:]})

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
        ToolDef(name="read_plugin_source", description="承認済み plugin の読取専用スナップショットを読む。",
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"]},
                func=read_plugin_source),
        ToolDef(name="run_plugin_tests", description="候補の test_plugin.py を回す (参考結果)。",
                parameters={"type": "object",
                            "properties": {"name": {"type": "string"}},
                            "required": ["name"]},
                func=run_plugin_tests),
    ]
