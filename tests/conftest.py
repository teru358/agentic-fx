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

from pathlib import Path
from urllib.parse import urlsplit

import pytest

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
