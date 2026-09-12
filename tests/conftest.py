"""テストスイート全体のネットワーク隔離ピン (2026-08-16)。

`WorkerRunner` は実 subprocess (`mission_worker`) を起動し、**子プロセスは
親から handshake で渡された `settings.llama_swap.base_url` へ本当に HTTP
POST する**。親側で FakeRunner を差し替えたつもりでも、別の loop
(`ReflectionCycle` など) が本物の `WorkerRunner` を握っていれば子は実
llama-swap を叩く — 実際に
`tests/test_e2e_worker_isolation.py::test_funds_protection_continues_during_
blocked_mission` が :8080 へ 1 スイートあたり 1 件・約 28 秒の POST を
出していた (llama-swap の journal で実測)。

**この検査は親プロセスにしか置けない** (子は別プロセスなので autouse
fixture が届かない)。そこで `WorkerRunner.run` = 起動引数が確定する唯一の
地点で `base_url` を見る。子側の遮断そのものではなく、「子に実アドレスを
渡そうとした瞬間に落とす」構造ピンであることを明記しておく。

**例外を投げるだけでは足りない (実測)**: `WorkerRunner.run` は
`MissionSupervisor` の**別スレッド**から呼ばれることがあり、そこで送出した
`AssertionError` は Mission の失敗として飲み込まれてテストは green のまま
だった。違反を記録して **fixture の teardown (= メインスレッド)** で落とす。

既知の穴 (net-isolation-probe.md §5.4、対処は裁定次第):

- この pin は親プロセスの `WorkerRunner.run` にしか効かない。テストが
  `subprocess.Popen([sys.executable, "-m", "agentic_fx.mission_worker"], ...)`
  を直に呼ぶ経路 (`tests/test_improve_profile_isolation.py::
  test_real_improve_worker_reaches_ready` 等) は素通りする。
- ポート denylist は「実 llama-swap の待受ポート」という環境事実に依存する。
  下記のとおり `config/settings.yaml.example` から導出して drift を防いで
  いるが、導出に失敗した場合のフォールバック値 (`{"8080"}`) 自体は環境
  事実のハードコードのままである。
- teardown 時点の `violations` を読むので、teardown より後まで生きている
  スレッドからの spawn は取りこぼす。
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest


def _new_untracked(before: "set[str] | None", after: "set[str] | None") -> set[str]:
    """検収是正 C3 (test-hygiene 2026-09-12):
    `_guard_repo_root_has_no_new_untracked_files` の判定本体を切り出した
    もの。**新規に増えた** untracked 名だけを返す — `before != after` で
    判定すると、テスト中に untracked ファイルが**減った**場合 (別の
    fixture が偶然 cleanup した等) にも fail してしまい、文言「新規…
    残った」と矛盾する。`before`/`after` のどちらかが `None` (git が
    使えず検査不能) なら空集合を返す (呼び出し側は None を「検査不能」
    として scope 外に扱う)。"""
    if before is None or after is None:
        return set()
    return after - before


def _top_level_untracked(porcelain_text: str) -> set[str]:
    """ローカル 1 周目 pin 是正 (test-hygiene 2026-09-12): `git status
    --porcelain --ignored=no -- .` の生テキストから、repo root 直下
    (非再帰) の untracked (`??`) エントリ名だけを抽出する純関数。
    `_guard_repo_root_has_no_new_untracked_files` の判定本体からこの
    解析部分だけを切り出し、`git` を実行せずに単体で pin できるように
    する。

    - `??` (untracked) 以外の行 (` M tracked.py` の変更・`!! ignored`
      の無視ファイル等) は対象外。
    - path がサブディレクトリ配下 (`/` を含む、末尾 `/` 自体は許容 —
      新規ディレクトリそのものは対象) なら対象外 (非再帰の対象外)。
    - git が二重引用符でクォートした path (空白・非 ASCII 等を含む場合)
      は前後のクォートだけを外す (エスケープシーケンスの解釈はしない —
      本 pin が扱う入力に現れない)。
    """
    names: set[str] = set()
    for line in porcelain_text.splitlines():
        if len(line) < 4 or line[:2] != "??":
            continue
        path = line[3:]
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        if "/" in path.rstrip("/"):
            continue  # サブディレクトリ配下 — 非再帰の対象外
        names.add(path)
    return names


#: 実 llama-swap の差し替え先 (即 ECONNREFUSED)。
#: `tests/test_e2e_worker_isolation.py` と
#: `tests/test_improve_profile_isolation.py` の双方が使う共有定数。
_LLAMA_SWAP_UNREACHABLE_URL = "http://127.0.0.1:1/v1"
#: settings.yaml.example の YAML テキストを直接置換する側 (test_e2e_worker_
#: isolation.py) が使う表記。
_LLAMA_SWAP_UNREACHABLE = f'base_url: "{_LLAMA_SWAP_UNREACHABLE_URL}"'

#: 実 llama-swap の待受ポート。テストがここへ向いた設定で worker を起動する
#: ことは、いかなる理由があっても許さない (外部プロセスを巻き込むため)。
#: `config/settings.yaml.example` の `llama_swap.base_url` から導出した値を
#: union する (example が変わっても drift しない)。導出に失敗した場合は
#: `{"8080"}` のみにフォールバックする。
_FORBIDDEN_PORTS = {"8080"}
try:
    from agentic_fx.config import load_settings as _load_settings

    _example_settings = _load_settings(
        Path(__file__).resolve().parents[1] / "config"
        / "settings.yaml.example")
    _example_port = urlsplit(_example_settings.llama_swap.base_url).port
    if _example_port is not None:
        _FORBIDDEN_PORTS = _FORBIDDEN_PORTS | {str(_example_port)}
except Exception:  # noqa: BLE001 — 導出できなければ {"8080"} のみで続行
    # settings.yaml.example が読めない/形式が変わった等。既知のポート値
    # {"8080"} だけを denylist に残し、pin 自体は機能させ続ける。
    pass
_FORBIDDEN_PORTS = frozenset(_FORBIDDEN_PORTS)


@pytest.fixture(autouse=True, scope="session")
def _isolate_mission_transcripts_default_dir(tmp_path_factory):
    """段A (mission transcript 常時保存) の既定保存先は `CliRunner` が
    `<repo>/logs/mission-transcripts/` を `__file__` から導出する
    (`cli_runner._TRANSCRIPT_DIR_DEFAULT`)。`CliRunner.run()` を呼ぶ
    どのテストも既定のままだと実リポジトリの `logs/` に書き込んでしまう
    ── `_guard_real_data_dir_is_never_touched` と同じ「テストが実
    リポジトリ資源を絶対座標で触る」事故クラス。セッション全体で
    その属性自体を隔離先へ差し替える (個別テストは transcript_dir= を
    渡す必要が無くなる)。

    T1(a) 是正 (test-hygiene 設計書 2026-09-12): この monkeypatch は
    **親プロセス**の属性しか差し替えられず、`WorkerRunner` が実
    `mission_worker` 子プロセスを起動するテスト (`worker_runner.
    _mission_worker_env` 経由) や `_bootstrap_improve_profile` を
    別プロセスで直接呼ぶ bootstrap probe (`tests/test_mission_worker.py`)
    には届かない。`os.environ["AGENTIC_FX_MISSION_TRANSCRIPTS_DIR"]` も
    同じ隔離先へ合わせて設定する — `cli_runner._TRANSCRIPT_DIR_DEFAULT`
    の初期化式がこの環境変数を読むため、子プロセス側で `cli_runner` が
    (再) import されたときも同じ隔離先を使う。"""
    import os

    from agentic_fx.runners import cli_runner as _cli_runner_mod

    original = _cli_runner_mod._TRANSCRIPT_DIR_DEFAULT
    isolated = tmp_path_factory.mktemp("mission-transcripts-default")
    _cli_runner_mod._TRANSCRIPT_DIR_DEFAULT = isolated
    original_env = os.environ.get("AGENTIC_FX_MISSION_TRANSCRIPTS_DIR")
    os.environ["AGENTIC_FX_MISSION_TRANSCRIPTS_DIR"] = str(isolated)
    yield
    _cli_runner_mod._TRANSCRIPT_DIR_DEFAULT = original
    if original_env is None:
        os.environ.pop("AGENTIC_FX_MISSION_TRANSCRIPTS_DIR", None)
    else:
        os.environ["AGENTIC_FX_MISSION_TRANSCRIPTS_DIR"] = original_env


@pytest.fixture(autouse=True, scope="session")
def _guard_real_mission_transcripts_dir_is_never_touched():
    """`_isolate_mission_transcripts_default_dir` は **親プロセス**の
    `cli_runner._TRANSCRIPT_DIR_DEFAULT` しか差し替えられない。
    `subprocess.Popen([sys.executable, "-m", "agentic_fx.mission_worker"],
    ...)` で本物の子プロセスを起動するテスト
    (`tests/test_improve_profile_isolation.py::
    test_real_improve_worker_reaches_ready` 等、`_forbid_worker_spawn_
    against_real_llama_swap` の docstring が既に名指ししている穴) は
    別プロセスで `cli_runner` を再 import するため、この monkeypatch は
    素通りする。子が実際に `CliRunner.run()` まで到達すれば実
    `<repo>/logs/mission-transcripts/` へ書く経路が残る —
    `_guard_real_data_dir_is_never_touched` と同じ「テストが実リポジトリ
    資源を絶対座標で触る」事故クラスなので、同じ形 (session 前後の
    スナップショット比較) で検査する。"""
    real_dir = Path(__file__).resolve().parents[1] / "logs" / "mission-transcripts"

    def _sig():
        if not real_dir.is_dir():
            return None
        return sorted(p.name for p in real_dir.iterdir())

    before = _sig()
    yield
    after = _sig()
    if before != after:
        pytest.fail(
            f"実 {real_dir} がテスト実行中に変更された: {before} -> {after}。"
            "本物の mission_worker 子プロセスを起動するテストが CliRunner."
            "run() まで到達し、既定の transcript 保存先 (親プロセスの "
            "monkeypatch が届かない別プロセス) へ書き込んだ可能性がある。",
            pytrace=False)


@pytest.fixture(autouse=True, scope="session")
def _guard_repo_root_has_no_new_untracked_files():
    """T1(b) 是正 (test-hygiene 設計書 2026-09-12): テストが repo root
    直下 (worktree root 直下、**再帰しない** — サブディレクトリの意図した
    一時ファイルまで拾うと fail closed が過検出になる) に production
    ファイル (`mcp.json` / `schema.json` / `prompt.txt` 等) を残す事故の
    構造ピン。`_guard_real_mission_transcripts_dir_is_never_touched` /
    `_guard_real_data_dir_is_never_touched` と同じ「session 前後の
    スナップショット比較・fail closed」の形を、repo root 直下の
    untracked ファイル全般に広げる。

    `git status --porcelain --ignored=no -- .` の top-level (`/` を
    含まない path) かつ `??` (untracked) 行だけを対象にする — 追跡済み
    ファイルへの変更や `.gitignore` 済みパスは対象外 (それらは意図した
    運用上の変化でありこの pin の対象外)。"""
    repo_root = Path(__file__).resolve().parents[1]

    def _sig():
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain", "--ignored=no", "--", "."],
                cwd=str(repo_root), capture_output=True, text=True,
                timeout=30, check=True)
        except (OSError, subprocess.CalledProcessError):
            # git が使えない環境では検査できない — fail open ではなく
            # 「検査不能」を記録するだけに留め、無関係な環境要因でスイート
            # 全体を落とさない。
            return None
        return _top_level_untracked(result.stdout)

    before = _sig()
    yield
    after = _sig()
    added = _new_untracked(before, after)
    if added:
        pytest.fail(
            f"repo root 直下に新規 untracked ファイルが残った: {sorted(added)}。"
            "テストが production ファイルを cwd (repo root) に書いている "
            "可能性がある — workdir を tmp_path へ隔離すること "
            "(test-hygiene T1(b))。",
            pytrace=False)


@pytest.fixture(autouse=True, scope="session")
def _guard_real_data_dir_is_never_touched():
    """実機 E2E (2026-08-30) の事故 pin: `test_run_gate_pytest_cannot_open_
    agentic_db` (旧実装) が実リポジトリの `data/agentic.db` を上書き→unlink
    し、フルスイートを repo cwd で回すたびに実 DB (取引・承認・mission の
    全履歴) が消えていた。テストが実 `data/` を絶対座標で触る事故は
    どのテストからでも起こりうるので、session 全体を挟んで (inode, size,
    mtime) の不変を検査する。変化していたら「どのテスト群を疑うか」を
    添えて session 終端で落とす。"""
    real_db = Path(__file__).resolve().parents[1] / "data" / "agentic.db"

    def _sig():
        try:
            st = real_db.stat()
            return (st.st_ino, st.st_size, st.st_mtime_ns)
        except FileNotFoundError:
            return None

    before = _sig()
    yield
    after = _sig()
    if before != after:
        pytest.fail(
            f"実 data/agentic.db がテスト実行中に変更された: {before} -> {after}。"
            "テストが実リポジトリの data/ を絶対座標で触っている "
            "(2026-08-30 の gate_pytest 事故の再演)。tmp_path / mkdtemp へ隔離すること。",
            pytrace=False)


@pytest.fixture(autouse=True)
def _restore_tmp_path_writable(request):
    """テストが tmp_path 配下に残す読み取り専用ツリー (0500 dir / 0400 file —
    _snapshot_src / version-store の pin) を teardown で書き込み可に戻す。

    戻さないと pytest の basetemp 掃除 (古い basetemp を `garbage-<uuid>` に
    rename → rmtree) が EACCES で失敗し、`/tmp/pytest-of-<user>/garbage-*`
    が世代ごとに残る (2026-09-05 実測: 95 世代 × 250MB = 12GB で /tmp の
    クォータを使い切り、Claude Code の Bash ツールが全滅した)。"""
    yield
    tmp_path = request.node.funcargs.get("tmp_path") if hasattr(
        request.node, "funcargs") else None
    if tmp_path is None or not tmp_path.exists():
        return
    import os
    for dirpath, dirnames, filenames in os.walk(tmp_path):
        try:
            os.chmod(dirpath, 0o700)
        except OSError:
            pass
        for fn in filenames:
            fp = os.path.join(dirpath, fn)
            if os.path.islink(fp):
                continue
            try:
                os.chmod(fp, 0o600)
            except OSError:
                pass


@pytest.fixture(autouse=True)
def _forbid_worker_spawn_against_real_llama_swap(request, monkeypatch):
    # Task13 Step5b (`@pytest.mark.realbackend`): この marker が付いた
    # テストは「実 llama-swap を意図的に叩く」ことそのものが目的の opt-in
    # テストであり (既定スイートからは `pyproject.toml` の addopts で除外
    # 済み、人間が `-m realbackend` を明示しない限り走らない)、この pin を
    # 適用すると本来の目的を達成できない。marker があるテストだけこの
    # autouse ガードを素通りさせる。
    if request.node.get_closest_marker("realbackend") is not None:
        yield
        return

    import subprocess

    from agentic_fx.runners import worker_runner as wr_mod
    from agentic_fx.runners.worker_runner import WorkerRunner

    original_run = WorkerRunner.run
    # **テスト本体が差し替える前**の本物を掴んでおく (fixture は test 本体より
    # 先に走る)。FakeChild 経由のテストは `wr_mod.subprocess.Popen` を
    # 差し替えるので、この同一性比較で「実 spawn かどうか」を判定できる。
    # `wr_mod.subprocess` はグローバルな `subprocess` モジュールそのものなので、
    # 実行時に `subprocess.Popen` と比べても常に一致してしまう (無意味) —
    # **必ずこの時点の値と比べること**。
    genuine_popen = subprocess.Popen

    violations: list[str] = []

    def guarded_run(self, mission):
        # FakeChild 経由のテスト (`subprocess.Popen` を差し替えている) は
        # 子を作らないので検査対象外。**実 spawn が起きる場合だけ**見る。
        if wr_mod.subprocess.Popen is not genuine_popen:
            return original_run(self, mission)

        base_url = self._settings.llama_swap.base_url
        port = urlsplit(base_url).port
        if port is not None and str(port) in _FORBIDDEN_PORTS:
            violations.append(base_url)
            # 記録だけでなく実 spawn も止める (これが遮断の実体)。呼び出し元が
            # supervisor スレッドなら握り潰されるので、判定は teardown で行う。
            raise AssertionError(_message(base_url))
        return original_run(self, mission)

    monkeypatch.setattr(WorkerRunner, "run", guarded_run)
    yield
    if violations:
        pytest.fail(_message(violations[0]), pytrace=False)


def _message(base_url: str) -> str:
    return ("テストが実 llama-swap を叩く設定で mission_worker を起動しようと "
            f"している (llama_swap.base_url={base_url!r})。到達不能アドレス "
            "(例 http://127.0.0.1:1/v1) に差し替えるか、build_app に runner= "
            "で fake を注入すること。")
