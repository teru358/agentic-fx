"""worker_isolation を実子 process で動かす helper (テストが `python -P <this> <cfg>` で起こす)。

protocol は通さない。子は `prepare_isolation()` → (handshake を cfg から受け取る) →
`isolate()` を直接呼び、結果を protocol fd (退避した元の stdout) に JSON 1 行ずつ書く。
隔離段の失敗は `isolate()` 自身が事前生成の `sandbox_ready ok:false` 行を同じ fd に書いて
終了する。

cfg (argv[1] の JSON):
- `main_dir`、`handshake`: `isolate()` に渡す値
- `repo_root`、`home`: 省略可。guarded root の基準
- `pre`: `prepare_isolation()` の前に exec する code
- `mid`: `prepare_isolation()` の後、`isolate()` の前に exec する code (`cfg` を書き換え可)
- `post`: 隔離の後に exec する code。`iso`・`out`・`emit`・`wait_parent` が使える
- `isolate`: false なら `isolate()` を呼ばない (隔離なしの対照)

隔離後は通常の interpreter 終了処理が表の外の syscall を呼び得るので、必ず `os._exit` で終える。
"""
import json
import os
import resource
import sys
import traceback

# core を残さない。hard は残し、isolate() が自分で (0, 0) にすることを試せるようにする
resource.setrlimit(resource.RLIMIT_CORE, (0, resource.getrlimit(resource.RLIMIT_CORE)[1]))
cfg = json.loads(sys.argv[1])

from agentic_fx.plugin import worker_isolation as wi  # noqa: E402

ns = {"wi": wi, "cfg": cfg, "os": os, "sys": sys, "json": json, "out": {}, "iso": None}


def emit(obj):
    wi._write_all(ns["proto"], (json.dumps(obj, sort_keys=True) + "\n").encode("utf-8"))


def wait_parent():
    """親が 1 行書くまで待つ (隔離後に親がファイルを書き換える試験用)。"""
    buf = b""
    while not buf.endswith(b"\n"):
        b = os.read(0, 1)
        if not b:
            break
        buf += b
    return buf.decode("ascii", "replace").strip()


ns["emit"] = emit
ns["wait_parent"] = wait_parent
ns["proto"] = 1

exec(cfg.get("pre", ""), ns)
prepared = wi.prepare_isolation()
ns["prepared"] = prepared
ns["proto"] = prepared.protocol_fd
exec(cfg.get("mid", ""), ns)

if cfg.get("isolate", True):
    from pathlib import Path

    iso = wi.isolate(prepared, cfg["handshake"], cfg["main_dir"],
                     repo_root=Path(cfg["repo_root"]) if cfg.get("repo_root") else None,
                     home=Path(cfg["home"]) if cfg.get("home") else None)
    ns["iso"] = iso
    emit({"phase": "isolated", "line": iso.sandbox_ready_line(os.getpid()).decode("ascii")})

try:
    exec(cfg.get("post", ""), ns)
except BaseException:  # noqa: BLE001
    emit({"phase": "error", "tb": traceback.format_exc()})
    os._exit(3)
emit({"phase": "done", "out": ns["out"]})
os._exit(0)
