"""共通 launcher (設計書 §1.1-1)。

multi-threaded なプロセス (service / mission_worker) では Python
`preexec_fn` を使わない。子の起動前処理 (PDEATHSIG・expected-parent
再照合・任意 rlimit) が要るときは、常にこのモジュールが組み立てる
単一スレッドの `python -c` 子プロセスを経由し、その launcher が前処理を
してから `os.execv` する。CLI 起動 (Task 2/3) と gate pytest 起動
(束 B Task 6) の両方から使う共通実装。

launcher 本体は文字列定数として本モジュールに持ち、それ自体は
モジュールとして import されない (`python -c "<source>"` の引数として
渡すだけ)。
"""
from __future__ import annotations

import json
from pathlib import Path

_LAUNCHER_SOURCE = """
import json, os, resource, sys

expected_parent_pid = int(sys.argv[1])
rlimits_json = sys.argv[2]
argv = sys.argv[3:]

try:
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_PDEATHSIG = 1
    SIGKILL = 9
    libc.prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0)
except Exception:
    os._exit(1)

if os.getppid() != expected_parent_pid:
    os._exit(1)

if rlimits_json:
    rlimits = json.loads(rlimits_json)
    for name, (soft, hard) in rlimits.items():
        resource.setrlimit(getattr(resource, name), (soft, hard))

os.execv(argv[0], argv)
"""


def build_launcher_argv(
    expected_parent_pid: int,
    argv: list[str],
    *,
    rlimits: dict[str, tuple[int, int]] | None = None,
) -> list[str]:
    """`[sys.executable, "-c", <launcher source>, str(expected_parent_pid),
    <json-encoded rlimits or "">, *argv]` を組み立てる。

    `argv` は解決済み絶対パスのみ (呼び出し側の起動時検査が済んでいる
    前提)。相対パスを渡すのは呼び出し側の誤りであり構築時点で拒否する。
    """
    import sys as _sys

    if not argv:
        raise ValueError("argv must not be empty")
    if not Path(argv[0]).is_absolute():
        raise ValueError(f"launcher argv[0] must be absolute: {argv[0]!r}")
    rlimits_json = json.dumps(rlimits) if rlimits else ""
    return [_sys.executable, "-c", _LAUNCHER_SOURCE, str(expected_parent_pid),
            rlimits_json, *argv]
