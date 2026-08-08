"""単一インスタンス保証 (プラン8, 裁定書 FC-2)。

`missions.recover_interrupted` は起動時に `status='running'` の全 mission
を無条件に `'interrupted'` へ終端する。同じ DB ディレクトリに対する
二重起動があると、後発プロセスが先発の稼働中 Mission を誤って終端し、
claim 済み signal を横取り requeue しかねない (実 grep で確認済み —
起動経路にプロセス排他が一切無かった)。DB と同一ディレクトリの lock
file への `flock(LOCK_EX | LOCK_NB)` で単一インスタンスを強制する
(fail closed — 取得できなければ起動そのものを中止する)。
"""
from __future__ import annotations

import fcntl
from pathlib import Path
from typing import IO


class InstanceAlreadyRunning(Exception):
    """同じ DB ディレクトリに対する別プロセスが既に instance lock を
    保持している (flock 取得失敗)。"""


def acquire_instance_lock(db_dir: Path) -> IO:
    """`db_dir` 直下の `instance.lock` を排他 lock する。

    戻り値のファイルオブジェクトは **呼び出し元プロセスの生涯にわたって
    保持**すること — close (または GC で暗黙 close) されると lock が
    解放される。呼び出し元 (`build_app`) は `App.instance_lock` として
    保持し、`App.close()` (Task 19) で明示的に close して解放する。
    """
    db_dir.mkdir(parents=True, exist_ok=True)
    lock_path = db_dir / "instance.lock"
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        raise InstanceAlreadyRunning(
            f"another agentic-fx process already holds the instance lock "
            f"at {lock_path} — refusing to start (fail closed, FC-2)") from e
    return fh
