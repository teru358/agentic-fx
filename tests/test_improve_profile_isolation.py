"""improve worker profile の権限境界 (プラン8, 設計書 §4.6)。"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_fx.core.landlock import is_available as landlock_available


def test_bootstrap_improve_profile_raises_when_landlock_unavailable(monkeypatch, tmp_path):
    import agentic_fx.mission_worker as mw_mod

    monkeypatch.setattr(mw_mod.landlock, "is_available", lambda: False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match="Landlock"):
        mw_mod._bootstrap_improve_profile()


def test_bootstrap_does_not_call_restrict_to_when_unavailable(monkeypatch, tmp_path):
    """事前チェック (`if not landlock.is_available()`) の**存在意義**を pin する。

    `restrict_to` 内部にも同じ判定があるため、事前チェックを削除しても
    `LandlockUnavailable` → `RuntimeError` の正規化 (下のテスト) によって
    同じ例外型になり、`test_..._raises_when_landlock_unavailable` だけでは
    削除変異が生存する (指揮者が実測)。事前チェックが守っているのは
    「**利用不能と分かっている状態で syscall を撃たない**」ことなので、
    そこを直接 assert する。

    この関数を in-process で呼んでよいのは、`is_available()` が False で
    `restrict_to` へ到達しない (= このプロセスが Landlock されない) 経路に
    限られる。
    """
    import agentic_fx.mission_worker as mw_mod

    calls: list[object] = []
    monkeypatch.setattr(mw_mod.landlock, "is_available", lambda: False)
    monkeypatch.setattr(mw_mod.landlock, "restrict_to",
                        lambda **kw: calls.append(kw))
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="Landlock"):
        mw_mod._bootstrap_improve_profile()

    assert calls == [], (
        "is_available() が False なのに restrict_to を呼んでいる — "
        "事前チェックが機能していない")


def test_bootstrap_normalizes_landlock_unavailable_from_restrict_to(monkeypatch, tmp_path):
    """`restrict_to` が `LandlockUnavailable` を送出したときも `RuntimeError`
    に正規化されることを pin する (指揮者の着手前検証 (2))。

    `LandlockUnavailable` は `Exception` 直系で `RuntimeError` ではない
    (`core/landlock.py`)。正規化する `try/except` を外すと、improve worker の
    「Landlock が唯一の FS 境界だから fail closed」という契約が、呼び出し元から
    見て別の例外型で漏れる。

    `restrict_to` を差し替えているのでこのプロセスは Landlock されない。
    """
    import agentic_fx.mission_worker as mw_mod
    from agentic_fx.core.landlock import LandlockUnavailable

    def boom(**kw):
        raise LandlockUnavailable("syscall failed (simulated)")

    monkeypatch.setattr(mw_mod.landlock, "is_available", lambda: True)
    monkeypatch.setattr(mw_mod.landlock, "restrict_to", boom)
    monkeypatch.chdir(tmp_path)

    with pytest.raises(RuntimeError, match="Landlock restriction failed") as exc:
        mw_mod._bootstrap_improve_profile()

    assert isinstance(exc.value.__cause__, LandlockUnavailable)


_ISOLATION_PROBE_SCRIPT = textwrap.dedent("""
    import os, sqlite3, sys
    from pathlib import Path
    os.chdir(sys.argv[2])  # WorkerRunner が cwd= に渡す専用空 workdir を模す
    from agentic_fx.mission_worker import _bootstrap_improve_profile
    _bootstrap_improve_profile()

    data_dir = Path(sys.argv[1])
    workdir = Path(sys.argv[2])
    results = {}

    # 裁定書 FC-5 (b): run_holdout_gate の import 成功を別 assertion に
    # 分離する (import できることと実行できないことを混同しない)。
    try:
        from agentic_fx.backtest.holdout import run_holdout_gate  # noqa: F401
        results["holdout_import"] = "ok"
    except Exception as e:
        results["holdout_import"] = f"FAILED: {type(e).__name__}: {e}"

    # ①data/agentic.db 絶対パス open 失敗 — 生 OS エラーは PermissionError
    # (Task 8 で実測確認済みの errno 13)。
    try:
        open(str(data_dir / "agentic.db"))
        results["open_db"] = "UNEXPECTED_SUCCESS"
    except PermissionError:
        results["open_db"] = "blocked"
    except Exception as e:  # noqa: BLE001 — 想定外の例外型は区別して記録する
        results["open_db"] = f"UNEXPECTED_EXCEPTION_TYPE: {type(e).__name__}: {e}"

    # ②data/ 列挙失敗
    try:
        os.listdir(str(data_dir))
        results["list_data_dir"] = "UNEXPECTED_SUCCESS"
    except PermissionError:
        results["list_data_dir"] = "blocked"
    except Exception as e:  # noqa: BLE001
        results["list_data_dir"] = f"UNEXPECTED_EXCEPTION_TYPE: {type(e).__name__}: {e}"

    # ③run_holdout_gate を呼んでもデータ到達不能で失敗 — 実測により
    # sqlite3.connect() 経由の失敗は PermissionError ではなく
    # sqlite3.OperationalError("unable to open database file") である
    # ことを確認済み。ここを PermissionError で判定すると (FC-5 が指摘
    # した反証不能な広すぎる except と同じ穴になるため) 明示的に
    # OperationalError + メッセージ内容まで確認する。
    try:
        conn = sqlite3.connect(str(data_dir / "agentic.db"))
        conn.execute("SELECT 1")
        conn.close()
        results["holdout_data_reachable"] = "UNEXPECTED_SUCCESS"
    except sqlite3.OperationalError as e:
        if "unable to open database file" in str(e):
            results["holdout_data_reachable"] = f"blocked: {type(e).__name__}"
        else:
            results["holdout_data_reachable"] = (
                f"UNEXPECTED_OPERATIONAL_ERROR_MESSAGE: {e}")
    except Exception as e:  # noqa: BLE001 — 想定外の例外型は区別して記録する
        results["holdout_data_reachable"] = f"UNEXPECTED_EXCEPTION_TYPE: {type(e).__name__}: {e}"

    # 裁定書 FC-5 (d) positive control: allowlist 内は実際に成功する
    # ことを積極的に示す (全滅していないことの証明)。
    try:
        (workdir / "probe.txt").write_text("ok")
        (workdir / "probe.txt").read_text()
        results["workdir_readwrite"] = "ok"
    except Exception as e:  # noqa: BLE001
        results["workdir_readwrite"] = f"UNEXPECTED_FAILURE: {type(e).__name__}: {e}"
    try:
        import agentic_fx
        Path(agentic_fx.__file__).read_text(encoding="utf-8")
        results["code_tree_read"] = "ok"
    except Exception as e:  # noqa: BLE001
        results["code_tree_read"] = f"UNEXPECTED_FAILURE: {type(e).__name__}: {e}"

    print(results)
""")


def test_improve_profile_cannot_reach_data_dir(tmp_path):
    """受入条件 §9-3: improve profile の実 worker プロセス内から
    ①data/agentic.db 絶対パス open 失敗 ②data/ 列挙失敗
    ③run_holdout_gate (import 可能・データ到達不能で実行失敗) を実測する。
    positive control (workdir 読書き・コードツリー読取) が成功することも
    確認し、「Landlock ポリシーが機能しているから遮断される」ことを
    「何かが壊れて全滅している」ことと区別する (裁定書 FC-5)。
    """
    if not landlock_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # 裁定書 FC-5 (a): 壊れた内容ではなく実際に有効な SQLite DB を seed
    # する — 「壊れているから失敗しただけ」と区別できるようにする。
    seed_conn = sqlite3.connect(str(data_dir / "agentic.db"))
    seed_conn.execute("CREATE TABLE t (x INTEGER)")
    seed_conn.commit()
    seed_conn.close()
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    result = subprocess.run(
        [sys.executable, "-c", _ISOLATION_PROBE_SCRIPT, str(data_dir), str(workdir)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "'holdout_import': 'ok'" in result.stdout
    assert "'open_db': 'blocked'" in result.stdout
    assert "'list_data_dir': 'blocked'" in result.stdout
    assert "'holdout_data_reachable': 'blocked" in result.stdout
    assert "'workdir_readwrite': 'ok'" in result.stdout
    assert "'code_tree_read': 'ok'" in result.stdout
    assert "UNEXPECTED" not in result.stdout
    # **このテスト単独の限界 (裁定書 FC-5 の指摘そのもの — 指揮者が実測)**:
    # probe は「子プロセスが自己申告した結果文字列」を見ているだけなので、
    # probe 側で `results["holdout_data_reachable"] = "blocked"` を無条件に
    # 固定する変異を入れても、このテストは green のままだった (段0 スイープ
    # M7 = SURVIVED)。probe の自己申告を信じてよいのは、
    # `test_real_improve_worker_reaches_ready` (実 worker が本当に起動する)
    # と `test_main_applies_landlock_bootstrap_before_running_improve_mission`
    # (`main()` が実際に Landlock を適用する) が別軸で成立している場合に限る。


def test_real_improve_worker_reaches_ready(tmp_path):
    """裁定書 F-8 (IM-2/P8-04) の回帰ピン: improve profile の実 worker
    プロセスが handshake 後に PermissionError で落ちず `ready` フレーム
    まで到達する。`ready` は LLM への実接続 (runner.run) より前に送出
    されるため、llama-swap が起動していない CI 環境でも検証できる。
    """
    if not landlock_available():
        pytest.skip("Landlock not available on this kernel/architecture")

    from agentic_fx.config import load_settings
    from agentic_fx.core.mission_protocol import read_frame, write_frame

    settings_path = (Path(__file__).resolve().parents[1] / "config"
                     / "settings.yaml.example")
    settings = load_settings(settings_path)
    workdir = tmp_path / "workdir"
    workdir.mkdir()

    proc = subprocess.Popen(
        [sys.executable, "-m", "agentic_fx.mission_worker"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=str(workdir), start_new_session=True)
    try:
        handshake = {
            "type": "handshake", "seq": 1,
            "expected_parent_pid": os.getpid(),
            "db_path": None, "plugins_dir": None,
            "settings": settings.model_dump(),
            "mission": {"prompt": "test", "tools": [],
                       "output_schema": {"type": "object"},
                       "max_turns": 1, "timeout_sec": 30},
            "worker_profile": "improve",
            "now": "2026-08-06T00:00:00+00:00",
        }
        write_frame(proc.stdin, handshake)
        frame = read_frame(proc.stdout)
        assert frame is not None, proc.stderr.read(4096)
        assert frame["type"] == "ready", (
            f"improve worker did not reach ready: {frame} "
            f"stderr={proc.stderr.read(4096) if proc.stderr else ''}")
        assert frame.get("ok") is True, frame
    finally:
        proc.kill()
        proc.wait(timeout=5.0)
