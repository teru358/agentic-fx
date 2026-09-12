"""improve worker profile の権限境界・実プロセス統合テスト
(設計書 §2.1 の 4 不変条件、§8.1 項目 12)。

測定 1 件 = 子プロセス 1 個 (Landlock 不可逆)。プラン 8 `plugins/` レイアウト
(`_staging/`/`_human/`/`_retired/`/`.versions/`/`.locks/`/`.history.git`) を
tmp_path 上に模擬し、improve worker からの到達不能を dirfd 検査で確認する。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from agentic_fx.core.landlock import is_available

_REPO_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not is_available(), reason="Landlock not available on this kernel/architecture")


def _mk_repo_layout(tmp_path: Path) -> dict:
    """**`workdir` は `repo/` から完全に独立させる**(advisor 指摘 —
    以前の稿は `workdir = staging_dir.parent.parent` = `repo/plugins` を
    cwd にしていたため、`_bootstrap_improve_profile` が `Path.cwd()` を
    rw allowlist に加える際に **`plugins/` 全体が書込可能になり**、
    `test_invariant2_cannot_listdir_plugins_root` を含む項目 12 の
    全テストが誤った理由で red/false-green になっていた)。

    **(Minor 16)** ここで作る `root / "data"` は `tmp_path` 配下の
    使い捨てディレクトリであり、`_guarded_data_dir()` が守る**実
    リポジトリ**の `data/` (`__file__` から `parents[2] / "data"` で
    導く固定座標) とは別物 — 本ファイルの不変条件 1 系テスト
    (`test_invariant1_*`) が測っているのは「(手組みの) allowlist に
    無いパスは EACCES になる」という Landlock 一般の挙動であって、
    `_assert_allowlist_excludes_data_dir` の防御 (allowlist の計算が
    実 `data/` を含んでしまう事故) を検査しているわけではない —
    その役目は既存 `tests/test_improve_profile_isolation.py::
    test_allowlist_never_covers_the_data_dir` が持つ。"""
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / "data" / "agentic.db").write_text("SQLITE-FAKE")
    plugins = root / "plugins"
    plugins.mkdir()
    (plugins / "_staging").mkdir()
    my_staging = plugins / "_staging" / "m-001"
    my_staging.mkdir(mode=0o700)
    other_staging = plugins / "_staging" / "m-002"
    other_staging.mkdir(mode=0o700)
    (plugins / "_human").mkdir()
    (plugins / "_human" / "some_plugin").mkdir()
    (plugins / "_retired").mkdir()
    (plugins / ".versions").mkdir()
    (plugins / ".locks").mkdir()
    (plugins / ".history.git").mkdir()
    (plugins / "approved_indicator").mkdir()  # 通常 plugin ディレクトリ (live)
    reports = root / "reports"
    reports.mkdir()
    workdir = tmp_path / "workdir"  # repo/plugins とは独立 (advisor 指摘の修正)
    workdir.mkdir()
    source_snapshot = workdir / "source"
    source_snapshot.mkdir(mode=0o500)
    return {"root": root, "my_staging": my_staging, "other_staging": other_staging,
           "reports": reports, "source_snapshot": source_snapshot, "workdir": workdir}


_EACCES_PRELUDE = textwrap.dedent("""
    import errno as _errno

    def _expect_eacces(fn, ok_label, fail_label):
        try:
            fn()
            print('FAIL: ' + fail_label)
        except OSError as e:
            if e.errno == _errno.EACCES:
                print('OK: ' + ok_label)
            else:
                print('FAIL: wrong errno ' + str(e.errno) + ' (expected EACCES=13)')
    """)


def _run_probe(script: str, *, staging_dir: Path, mission_id: str,
              source_snapshot_dir: Path, workdir: Path,
              backend: str = "local") -> subprocess.CompletedProcess:
    full = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(_REPO_ROOT / "src")!r})
        from agentic_fx.mission_worker import _bootstrap_improve_profile
        _bootstrap_improve_profile(
            backend={backend!r}, mission_id={mission_id!r},
            staging_dir={str(staging_dir)!r},
            source_snapshot_dir={str(source_snapshot_dir)!r},
            claude_bin=None, codex_bin=None)
    """) + "\n" + _EACCES_PRELUDE + "\n" + textwrap.dedent(script)
    # T1(a) 是正 (test-hygiene 設計書 2026-09-12): `_bootstrap_improve_
    # profile` は fail-closed 検査の前に (無条件で) transcript 保存先を
    # mkdir する。この probe は別プロセスなので `tests/conftest.py` の
    # 親プロセス monkeypatch が届かず、隔離用環境変数で子自身に伝える
    # 必要がある (`tests/test_mission_worker.py::_run_bootstrap_probe`
    # と同じ手当て)。
    env = {"PATH": "/usr/bin:/bin", "HOME": str(workdir / "home"),
           "AGENTIC_FX_MISSION_TRANSCRIPTS_DIR":
               str(workdir / "mission-transcripts-isolated")}
    return subprocess.run([sys.executable, "-c", full], cwd=str(workdir),
                          env=env,
                          capture_output=True, text=True, timeout=30)


# --- 不変条件 1: data/ に読み書きとも到達できない ---------------------------
# **errno を literal で検査する** (§8.1-12「open/write の期待 errno まで」)。
# Landlock は存在を隠さない (ENOENT にはならない) — 拒否は常に EACCES(13)。

def test_invariant1_cannot_open_agentic_db(tmp_path):
    layout = _mk_repo_layout(tmp_path)
    db_path = layout["root"] / "data" / "agentic.db"
    script = f"""
    _expect_eacces(lambda: open({str(db_path)!r}, 'rb'),
                  'agentic.db EACCES', 'opened agentic.db')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: agentic.db EACCES" in result.stdout


def test_invariant1_cannot_listdir_data(tmp_path):
    layout = _mk_repo_layout(tmp_path)
    data_dir = layout["root"] / "data"
    script = f"""
    import os
    _expect_eacces(lambda: os.listdir({str(data_dir)!r}),
                  'data/ EACCES', 'listed data/')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: data/ EACCES" in result.stdout


def test_invariant1_cannot_truncate_agentic_db(tmp_path):
    layout = _mk_repo_layout(tmp_path)
    db_path = layout["root"] / "data" / "agentic.db"
    script = f"""
    import os
    _expect_eacces(lambda: os.truncate({str(db_path)!r}, 0),
                  'truncate EACCES', 'truncated agentic.db')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: truncate EACCES" in result.stdout


# --- 不変条件 2: 書込可能パスは staging/workdir/dev のみ --------------------

def test_invariant2_cannot_listdir_plugins_root(tmp_path):
    """`plugins/` 自体は worker から不可視 — 最強の単一 assertion
    (どの allowlist にも `plugins/` 自体は入らない、§2.3)。"""
    layout = _mk_repo_layout(tmp_path)
    plugins_dir = layout["root"] / "plugins"
    script = f"""
    import os
    _expect_eacces(lambda: os.listdir({str(plugins_dir)!r}),
                  'plugins/ EACCES', 'listed plugins/')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: plugins/ EACCES" in result.stdout


def test_invariant2_own_staging_is_writable(tmp_path):
    """positive control: 自分の staging には書ける。"""
    layout = _mk_repo_layout(tmp_path)
    script = f"""
    from pathlib import Path
    (Path({str(layout['my_staging'])!r}) / 'plugin.py').write_text('x')
    print('OK: own staging writable')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: own staging writable" in result.stdout


def test_invariant2_other_missions_staging_is_unreachable(tmp_path):
    """他 Mission の staging (`_staging/m-002/`) には到達できない —
    staging を Mission id で分けず共有にする変異の killer (§2 変異表)。"""
    layout = _mk_repo_layout(tmp_path)
    script = f"""
    import os
    _expect_eacces(lambda: os.listdir({str(layout['other_staging'])!r}),
                  'other staging EACCES', 'listed other mission staging')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: other staging EACCES" in result.stdout


def test_invariant2_reports_dir_is_unreachable(tmp_path):
    layout = _mk_repo_layout(tmp_path)
    script = f"""
    import os
    _expect_eacces(lambda: os.listdir({str(layout['reports'])!r}),
                  'reports/ EACCES', 'listed reports/')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: reports/ EACCES" in result.stdout


# --- 不変条件 3: 従量課金経路が無い (env に鍵が無い) -------------------------

def test_invariant3_no_billing_keys_in_environ(tmp_path):
    """`os.environ` 自体を子の中で検査する (`/proc` 経由ではない —
    `/proc` は claude backend のときしか allowlist に無いため、
    `/proc/self/environ` を読む形で書くと codex/local では検査自体が
    vacuously pass してしまう)。

    **(着手前検証 Blocking 6 修正) 撤回する主張**: このテストは
    「不変条件 3 を pin する」とは言えない — `_run_probe` が渡す `env`
    はこのテストファイル自身が組み立てた辞書 (`{"PATH": ..., "HOME":
    ...}`) であり、本番の env builder
    (`worker_runner._mission_worker_env`/A-1 の scratch env) を一切
    通らない。ここで確認しているのは「テストが渡さなかったキーは
    `os.environ` に出てこない (Landlock 越しに親の env が漏れて増える
    ことはない) こと」に限られる smoke であり、production の
    allowlist に `ANTHROPIC_API_KEY` を混入させる変異はこのテストでは
    検出できない (その killer は `tests/runners/test_worker_runner.py::
    test_mission_worker_env_excludes_credentials_for_improve` — Task 1
    の担当 — が持つ)。"""
    layout = _mk_repo_layout(tmp_path)
    script = """
    import os
    leaked = [k for k in os.environ if 'API_KEY' in k or k in
             ('ANTHROPIC_API_KEY', 'OPENAI_API_KEY')]
    if leaked:
        print('FAIL: leaked keys ' + str(leaked))
    else:
        print('OK: no billing keys')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: no billing keys" in result.stdout


# --- 不変条件 4: 個人設定を継承しない -----------------------------------

def test_invariant4_home_is_scratch_not_real_home(tmp_path):
    """**(着手前検証 Blocking 6 修正) 撤回する主張**: 同じ理由 (上記
    `test_invariant3_no_billing_keys_in_environ` 参照) で、この
    テストが渡す `HOME` はテスト自身が組み立てた値であり、production
    の HOME 決定ロジック (§2.2 — A-1/D-10 の親側 env 構築責務) を
    通らない。ここで確認しているのは「`_bootstrap_improve_profile` が
    `HOME` を検査・書き換えない (env 非依存で Landlock だけを張る) 」
    ことに限られる smoke — `test_bootstrap_improve_profile_home_env_
    is_scratch_dir` (5-D) と同じ性質の重複であり、本番で実ホームが
    渡ってしまう変異はここでは検出できない。"""
    layout = _mk_repo_layout(tmp_path)
    scratch_home = layout["workdir"] / "home"
    scratch_home.mkdir(exist_ok=True)
    script = f"""
    import os
    assert os.environ.get('HOME') == {str(scratch_home)!r}, os.environ.get('HOME')
    print('OK: HOME is scratch')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: HOME is scratch" in result.stdout


def test_invariant4_real_claude_home_is_unreachable(tmp_path):
    layout = _mk_repo_layout(tmp_path)
    real_home = Path.home()
    if not (real_home / ".claude").exists():
        pytest.skip("no ~/.claude on this host to probe against")
    script = f"""
    import os
    _expect_eacces(lambda: os.listdir({str(real_home / '.claude')!r}),
                  'real ~/.claude EACCES', 'listed real ~/.claude')
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"], backend="claude")
    assert result.returncode == 0, result.stderr
    assert "OK: real ~/.claude EACCES" in result.stdout


# --- 項目 12: `_staging`/`_human`/`_retired`/`.versions`/`.locks`/`.history.git` ---

@pytest.mark.parametrize("subpath", [
    "_staging/m-002",     # 他 Mission の staging も含む (2 周目の pin)
    "_human/some_plugin",
    "_retired",
    ".versions",
    ".locks",
    ".history.git",
])
def test_item12_privileged_plugin_subdirs_are_unreachable(tmp_path, subpath):
    layout = _mk_repo_layout(tmp_path)
    target = layout["root"] / "plugins" / subpath
    script = f"""
    import os
    _expect_eacces(lambda: os.listdir({str(target)!r}),
                  'EACCES', 'listed ' + {subpath!r})
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: EACCES" in result.stdout


@pytest.mark.parametrize("subpath", [
    "_staging/m-002", "_human/some_plugin", "_retired", ".versions",
    ".locks", ".history.git", "approved_indicator",
])
def test_item12_privileged_plugin_subdirs_are_not_writable(tmp_path, subpath):
    """非可視だけでなく非書込であることも別軸で pin する
    (readdir を拒否されても write が別経路で通る実装ミスを検出)。"""
    layout = _mk_repo_layout(tmp_path)
    target_dir = layout["root"] / "plugins" / subpath
    target_dir.mkdir(parents=True, exist_ok=True)
    victim = target_dir / "evil.txt"
    script = f"""
    from pathlib import Path
    _expect_eacces(lambda: Path({str(victim)!r}).write_text('owned'),
                  'write EACCES', 'wrote into ' + {subpath!r})
    """
    result = _run_probe(script, staging_dir=layout["my_staging"],
                        mission_id="m-001", source_snapshot_dir=layout["source_snapshot"],
                        workdir=layout["workdir"])
    assert result.returncode == 0, result.stderr
    assert "OK: write EACCES" in result.stdout
