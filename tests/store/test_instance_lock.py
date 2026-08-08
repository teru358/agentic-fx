"""単一インスタンス保証 (プラン8, 裁定書 FC-2)。"""
from __future__ import annotations

import pytest

from agentic_fx.store.instance_lock import (
    InstanceAlreadyRunning, acquire_instance_lock,
)


def test_acquire_instance_lock_succeeds_when_uncontended(tmp_path):
    fh = acquire_instance_lock(tmp_path / "data")
    assert fh is not None
    fh.close()


def test_acquire_instance_lock_raises_when_already_held(tmp_path):
    """同一プロセス内でも、先に取得した lock file オブジェクトを close
    せずに 2 回目を取得しようとすると失敗する (flock は open file
    description 単位 — 2 回目の open は別の file description になる)。"""
    db_dir = tmp_path / "data"
    first = acquire_instance_lock(db_dir)
    try:
        with pytest.raises(InstanceAlreadyRunning):
            acquire_instance_lock(db_dir)
    finally:
        first.close()


def test_acquire_instance_lock_succeeds_again_after_release(tmp_path):
    db_dir = tmp_path / "data"
    first = acquire_instance_lock(db_dir)
    first.close()  # 解放

    second = acquire_instance_lock(db_dir)
    second.close()
