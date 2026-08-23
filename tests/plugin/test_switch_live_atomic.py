"""switch_live の原子性 pin (検収 B1、設計書 §5.1 手順 6)。

設計は「切替 (原子操作 1 回)」を要求する: temp symlink を作り、
`os.rename(temp, plugins/<name>)` の 1 rename だけで旧 symlink/absent から
新 symlink へ移る。「live が無い瞬間」が存在しないことが不変条件 (§5.3)。
段 0 変異 (i) は `switch_live` を `live.unlink() → live.symlink_to(new_target)
→ temp.unlink()` の非 atomic な 2 段に置き換えたが、既存テストは終状態が
rename と同一であるため区別できず全緑のまま survive した
(検収 acceptance-task11.md B1)。

本テストは (1) `os.rename` がちょうど 1 回、`(temp_path, live_path)` の
引数で呼ばれること (2) live パス自身に対する `unlink` が一切呼ばれない
こと の 2 点を pin する。2 段化変異は `live.unlink()` を呼び、かつ最終
遷移を `os.rename` ではなく `temp.unlink()` (自身への unlink であり live
ではない) で終えるため、(1)(2) いずれの assert も red になる。
"""
from __future__ import annotations

import os
from pathlib import Path

from agentic_fx.plugin import switch


def test_switch_live_is_a_single_atomic_rename_with_no_live_unlink(tmp_path, monkeypatch):
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    name = "sma"
    old_hash = "a" * 64
    new_hash = "b" * 64
    (plugins_root / ".versions" / name / old_hash).mkdir(parents=True)
    (plugins_root / ".versions" / name / new_hash).mkdir(parents=True)
    live = plugins_root / name
    live.symlink_to(f".versions/{name}/{old_hash}")

    rename_calls = []
    real_rename = os.rename

    def spy_rename(src, dst, *a, **kw):
        rename_calls.append((os.fspath(src), os.fspath(dst)))
        return real_rename(src, dst, *a, **kw)

    unlink_calls = []
    real_unlink = Path.unlink

    def spy_unlink(self, *a, **kw):
        unlink_calls.append(str(self))
        return real_unlink(self, *a, **kw)

    monkeypatch.setattr(switch.os, "rename", spy_rename)
    monkeypatch.setattr(Path, "unlink", spy_unlink)

    new_target = f".versions/{name}/{new_hash}"
    switch.switch_live(plugins_root, name, new_target=new_target, op_id=1)

    assert live.is_symlink()
    assert live.readlink().as_posix() == new_target

    temp_path_str = str(plugins_root / f".{name}.link-1")
    assert rename_calls == [(temp_path_str, str(live))], (
        "switch_live は temp symlink → live への os.rename をちょうど 1 回 "
        "だけ呼ぶこと (段 0 変異 (i): 2 段化すると unlink→symlink→temp.unlink "
        "になり、この形の rename 呼び出しが消える)")
    assert str(live) not in unlink_calls, (
        "live パス自身への unlink が発生した — 「live が無い瞬間」が生じる "
        "非 atomic な切替 (段 0 変異 (i))")


def test_switch_live_from_absent_is_also_a_single_rename(tmp_path, monkeypatch):
    """live が absent (初回切替) のときも同じ 1 rename 経路を通ること。"""
    plugins_root = tmp_path / "plugins"
    plugins_root.mkdir()
    name = "sma"
    new_hash = "c" * 64
    (plugins_root / ".versions" / name / new_hash).mkdir(parents=True)
    live = plugins_root / name

    rename_calls = []
    real_rename = os.rename

    def spy_rename(src, dst, *a, **kw):
        rename_calls.append((os.fspath(src), os.fspath(dst)))
        return real_rename(src, dst, *a, **kw)

    monkeypatch.setattr(switch.os, "rename", spy_rename)

    new_target = f".versions/{name}/{new_hash}"
    switch.switch_live(plugins_root, name, new_target=new_target, op_id=7)

    temp_path_str = str(plugins_root / f".{name}.link-7")
    assert rename_calls == [(temp_path_str, str(live))]
    assert live.is_symlink()
