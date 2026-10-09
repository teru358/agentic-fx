"""鍵 dir への到達を、worker と同じ制限を掛けた子 process の内側から確かめる。

usage: _keyprobe.py '<json>'
  json = {"profile": "improve" | "trade" | "plain", "files": [...], "dirs": [...], ...}
- improve: 本番の改善 worker と同じ ``_bootstrap_improve_profile`` (Landlock) を掛ける
- trade: 本番の trade worker と同じ資源制限だけを掛ける (trade worker に Landlock は無い)
- plain: 何も掛けない (利用者 shell と同じ)
結果は stdout の最後の行に JSON {"files": {path: "ok"|errno}, "dirs": {...}} で出す。
"""
from __future__ import annotations

import json
import os
import resource
import sys

resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _probe(path: str, listing: bool) -> object:
    try:
        if listing:
            os.listdir(path)
        else:
            os.close(os.open(path, os.O_RDONLY | os.O_CLOEXEC))
        return "ok"
    except OSError as exc:
        return exc.errno


def main() -> int:
    cfg = json.loads(sys.argv[1])
    profile = cfg["profile"]
    if profile == "improve":
        from agentic_fx.mission_worker import _bootstrap_improve_profile
        _bootstrap_improve_profile(
            backend=cfg["backend"], mission_id=cfg["mission_id"],
            staging_dir=cfg["staging_dir"], source_snapshot_dir=cfg["source_snapshot_dir"],
            claude_bin=None, codex_bin=None)
    elif profile == "trade":
        from agentic_fx.mission_worker import _set_resource_limits
        _set_resource_limits(**cfg["limits"])
    elif profile != "plain":
        raise SystemExit(f"unknown profile {profile}")
    out = {"files": {p: _probe(p, False) for p in cfg["files"]},
           "dirs": {p: _probe(p, True) for p in cfg["dirs"]}}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
