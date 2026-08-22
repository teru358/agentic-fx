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
        mw_mod._bootstrap_improve_profile(
            backend="local", mission_id="m-001",
            staging_dir=str(tmp_path / "m-001"),
            source_snapshot_dir=str(tmp_path / "src"),
            claude_bin=None, codex_bin=None)


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
        mw_mod._bootstrap_improve_profile(
            backend="local", mission_id="m-001",
            staging_dir=str(tmp_path / "m-001"),
            source_snapshot_dir=str(tmp_path / "src"),
            claude_bin=None, codex_bin=None)

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

    # (逸脱: プラン本文は「作成不要 — 到達前に例外になる経路」と書くが、
    # 5-D 実装では staging_dir の dirfd 再検証と source_snapshot_dir の
    # 実在確認が `restrict_to` 呼び出しより**前**にあるため、実際に到達
    # させるにはこの 2 つのディレクトリが存在している必要がある。ここで
    # は `restrict_to` の LandlockUnavailable → RuntimeError 正規化だけを
    # 検査したいので、staging_dir は再検証を通す 0700 で作る。)
    (tmp_path / "m-001").mkdir(mode=0o700)
    (tmp_path / "src").mkdir()

    with pytest.raises(RuntimeError, match="Landlock restriction failed") as exc:
        mw_mod._bootstrap_improve_profile(
            backend="local", mission_id="m-001",
            staging_dir=str(tmp_path / "m-001"),
            source_snapshot_dir=str(tmp_path / "src"),
            claude_bin=None, codex_bin=None)

    assert isinstance(exc.value.__cause__, LandlockUnavailable)


_ISOLATION_PROBE_SCRIPT = textwrap.dedent("""
    import os, sqlite3, sys
    from pathlib import Path
    os.chdir(sys.argv[2])  # WorkerRunner が cwd= に渡す専用空 workdir を模す
    mission_id = "iso-probe"
    staging = Path(sys.argv[2]) / "staging" / mission_id
    staging.mkdir(parents=True, mode=0o700)
    source_snapshot = Path(sys.argv[2]) / "source"
    source_snapshot.mkdir(mode=0o500)
    from agentic_fx.mission_worker import _bootstrap_improve_profile
    _bootstrap_improve_profile(
        backend="local", mission_id=mission_id, staging_dir=str(staging),
        source_snapshot_dir=str(source_snapshot), claude_bin=None, codex_bin=None)

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

    # **positive control 2 (レビュー 2 周目 /code-review の HIGH 指摘)**:
    # 名前解決が生きていること。allowlist から `/etc` を落とすと
    # `getaddrinfo` が gaierror になり、既定の `llama_swap.base_url`
    # (`http://localhost:8080/v1`) へ到達できず improve Mission が初回
    # ターンで必ず failed になる。**`ready` 到達だけを見るテストでは
    # この破壊を検出できない** (ready はその 1 行手前で送出される) ため、
    # probe 側で実際に名前解決まで踏む。
    try:
        import socket
        socket.getaddrinfo("localhost", 8080)
        results["name_resolution"] = "ok"
    except Exception as e:  # noqa: BLE001
        results["name_resolution"] = f"UNEXPECTED_FAILURE: {type(e).__name__}: {e}"

    # 名前解決だけでなく **実際に socket を張るところまで**踏む。
    # llama-swap が起動していない環境でも判定できるよう、
    # `ConnectionRefusedError` (= 解決も接続試行も成立した) は ok 扱いにし、
    # `gaierror` / `PermissionError` (= Landlock が経路を塞いだ) だけを
    # 失敗とする。これで「起動できる」ではなく「LLM へ到達できる」を測る。
    try:
        import socket
        socket.create_connection(("localhost", 8080), timeout=3).close()
        results["llm_endpoint_reachable"] = "ok"
    except ConnectionRefusedError:
        results["llm_endpoint_reachable"] = "ok (refused — 経路は生きている)"
    except Exception as e:  # noqa: BLE001
        results["llm_endpoint_reachable"] = f"UNEXPECTED_FAILURE: {type(e).__name__}: {e}"

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
    assert "'name_resolution': 'ok'" in result.stdout, (
        "Landlock 適用後に名前解決ができない — improve Mission は初回ターンで "
        "failed になる (allowlist から /etc が落ちていないか確認すること)")
    assert "'llm_endpoint_reachable': 'ok" in result.stdout, (
        "Landlock 適用後に LLM エンドポイントへ socket を張れない — "
        "improve Mission は初回ターンで failed になる")
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
    # **ネットワーク隔離** (2026-08-16, net-isolation-probe.md §5.4): この
    # テストは実 mission_worker を直に Popen するため `tests/conftest.py` の
    # `_forbid_worker_spawn_against_real_llama_swap` pin (WorkerRunner.run 経由
    # の spawn しか見ない) は素通りする。`ready` フレーム受領直後に kill する
    # ので現状 POST は出ないが、静かに漏れる穴を塞ぐため base_url をここでも
    # 到達不能アドレスへ差し替えておく。
    from tests.conftest import _LLAMA_SWAP_UNREACHABLE_URL

    settings = settings.model_copy(update={
        "llama_swap": settings.llama_swap.model_copy(
            update={"base_url": _LLAMA_SWAP_UNREACHABLE_URL}),
    })
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    staging = workdir / "staging" / "iso-ready-probe"
    staging.mkdir(parents=True, mode=0o700)
    source_snapshot = workdir / "source"
    source_snapshot.mkdir(mode=0o500)

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
            "mission_id": "iso-ready-probe",
            "staging_dir": str(staging),
            "source_snapshot_dir": str(source_snapshot),
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


def test_allowlist_never_covers_the_data_dir(monkeypatch, tmp_path):
    """**allowlist の計算結果そのものを不変条件として検査する** (プラン8 Task 18)。

    段0 変異スイープで、allowlist の計算を誤らせる変異が 2 つとも
    フルスイート 1683 件を green のまま通り抜けた (codex 1周目 #1/#2):

    - `WorkerRunner` の `Popen(..., cwd=workdir)` から `cwd=` を落とす
      → 子の cwd がリポジトリ root になり `data/` が **read-write** に
    - `code_root` を `src/` からリポジトリ root に広げる → `data/` が読取可能に

    probe テスト (`test_improve_profile_cannot_reach_data_dir`) が遮断を
    確認しているのは `tmp_path/data` であって**本物の `<repo>/data` ではない**
    ため、どちらの変異も検出できなかった。ここでは実際に計算された
    allowlist を捕まえ、`data/` の祖先が混ざらないことを直接見る。

    `restrict_to` を差し替えているのでこのプロセスは Landlock されない。
    """
    import agentic_fx.mission_worker as mw_mod

    captured: dict = {}
    monkeypatch.setattr(mw_mod.landlock, "is_available", lambda: True)
    monkeypatch.setattr(mw_mod.landlock, "restrict_to",
                        lambda **kw: captured.update(kw))
    monkeypatch.chdir(tmp_path)

    # Create staging and source_snapshot dirs for new signature
    staging = tmp_path / "staging" / "m-test"
    staging.mkdir(parents=True, mode=0o700)
    source = tmp_path / "source"
    source.mkdir(mode=0o500)

    mw_mod._bootstrap_improve_profile(
        backend="local", mission_id="m-test",
        staging_dir=str(staging),
        source_snapshot_dir=str(source),
        claude_bin=None, codex_bin=None)

    data_dir = mw_mod._guarded_data_dir()
    allowed = (list(captured["read_only_paths"]) + list(captured["read_write_paths"])
              + list(captured.get("execute_paths", [])))
    assert allowed, "allowlist が空 — restrict_to の呼び出しを捕まえられていない"
    for p in allowed:
        resolved = Path(p).resolve()
        # 祖先・一致・**子孫**の 3 方向すべて (レビュー 3 周目 sonnet: 2 周目で
        # ガード側に子孫棄却を足したのに、この assert が祖先/一致だけを見る
        # 古い形のまま残っていた — ガードと pin が片方だけ進んでいた)。
        assert (resolved != data_dir and resolved not in data_dir.parents
                and data_dir not in resolved.parents), (
            f"allowlist の {resolved} が {data_dir} を覆っている / 配下にある")
    # data_dir 自身が allowlist に含まれていないことの否定的確認だけだと、
    # 「allowlist が空でも通る」恒真に落ちるので、正の確認も置く。
    code_root = Path(mw_mod.__file__).resolve().parents[1]  # <repo>/src
    assert code_root in [Path(p).resolve() for p in captured["read_only_paths"]], \
        "コードツリー (src/) が read_only allowlist に入っていない"
    assert Path(tmp_path).resolve() in [
        Path(p).resolve() for p in captured["read_write_paths"]], \
        "専用 workdir (cwd) が read_write allowlist に入っていない"


def test_bootstrap_fails_closed_when_cwd_would_expose_data_dir(monkeypatch, tmp_path):
    """`Popen(cwd=...)` が専用 workdir でなくリポジトリ root になった場合
    (= codex 1周目 #1 の変異)、**起動を拒否する**ことを pin する。

    この経路が無いと、`cwd=` の指定漏れが `data/` を read-write allowlist に
    入れたまま静かに成立してしまう。
    """
    import agentic_fx.mission_worker as mw_mod

    repo_root = mw_mod._guarded_data_dir().parent
    monkeypatch.setattr(mw_mod.landlock, "is_available", lambda: True)
    monkeypatch.setattr(mw_mod.landlock, "restrict_to",
                        lambda **kw: pytest.fail(
                            "data/ を覆う allowlist で restrict_to を呼んでいる"))
    monkeypatch.chdir(repo_root)

    # Create staging and source_snapshot dirs
    # Note: workdir=cwd=repo_root (sibling to data/ — this should trigger allowlist error)
    staging = repo_root / "staging" / "m-test"
    staging.mkdir(parents=True, mode=0o700, exist_ok=True)
    source = repo_root  # source is workdir itself

    with pytest.raises(RuntimeError, match="would expose the history data"):
        mw_mod._bootstrap_improve_profile(
            backend="local", mission_id="m-test",
            staging_dir=str(staging),
            source_snapshot_dir=str(source),
            claude_bin=None, codex_bin=None)


def test_run_holdout_gate_requires_history_conn_keyword():
    """「import は可能・実行は不能」の意味論が寄りかかっている API 境界を pin
    する (codex 1周目 #4)。

    `run_holdout_gate` が `history_conn` を**必須の keyword-only 引数**として
    取ることが、「DB に到達できない improve worker では実行が必ず失敗する」の
    根拠そのもの。既定値が付く・関数内部で DB を探しにいく、といった退行が
    起きると遮断の意味論が静かに崩れる。
    """
    import inspect

    from agentic_fx.backtest.holdout import run_holdout_gate

    sig = inspect.signature(run_holdout_gate)
    param = sig.parameters["history_conn"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY, (
        "history_conn が keyword-only でなくなっている")
    assert param.default is inspect.Parameter.empty, (
        "history_conn に既定値が付いた — DB 接続なしで実行できてしまう")


def test_guard_rejects_allowlist_paths_under_the_data_dir():
    """`data/` の**配下**を allowlist に入れる経路も fail closed になること
    (レビュー 3 周目 sonnet)。

    2 周目 (`/code-review` の MEDIUM) でガードに子孫棄却を足したが、**その
    分岐だけを守るテストが無く、削除しても 68 件が green のままだった**
    (sonnet が実測)。ガードと pin が片方だけ進んでいた典型。

    実運用では workdir は `tempfile.TemporaryDirectory()` が `/tmp` 配下に
    作るのでこの分岐は発火しないが、`WorkerRunner` が将来 workdir の置き場
    を変えたときに効く最後の網なので、退行を検出できる形にしておく。
    """
    import agentic_fx.mission_worker as mw_mod
    from agentic_fx.core.landlock import _assert_allowlist_excludes_data_dir

    inside = mw_mod._guarded_data_dir() / "sub"
    with pytest.raises(RuntimeError, match="would expose the history data"):
        _assert_allowlist_excludes_data_dir(
            [inside], guarded_data_dir=mw_mod._guarded_data_dir())
    _assert_allowlist_excludes_data_dir(
        [Path("/usr/lib")], guarded_data_dir=mw_mod._guarded_data_dir())
