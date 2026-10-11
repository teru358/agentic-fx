"""改善 mission の CLI 隔離の安全性 canary (項目の固定、実隔離の下の実行、失敗の扱い)。

実隔離の試験は使い捨ての user service (`afxtest-<id>-*`) の中で、製品の WorkerRunner と実
mission worker を通して走らせる。触るのはその unit の cgroup だけ。
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

import pytest

from agentic_fx.core import improve_canary as C

SERVICE_HELPER = Path(__file__).with_name("_improve_isolation_service_helper.py")

#: 仕様 §8 手順 1 の列挙 (IPv4 / IPv6 の listen と、順・逆の port reuse は 2 項目ずつ)
SPEC_ITEMS = (
    "af_unix_denied", "disallowed_port", "listen_ipv4_eperm", "listen_ipv6_eperm",
    "clone_parent_eperm", "external_signal", "dev_tty", "cgroup_file",
    "shared_ro_fd_metadata", "subreaper", "proxy_external_connect", "port_reuse_forward",
    "port_reuse_reverse", "bucket_collision", "receiver_close_race",
)


def test_canary_items_are_pinned():
    assert C.CANARY_ITEMS == SPEC_ITEMS
    assert tuple(C.EXPECTED) == SPEC_ITEMS
    assert set(C.PROXY_ITEMS) < set(SPEC_ITEMS)
    # 各項目に実装 (CLI 役の試験 script か worker 役) がある
    probe_keys = {n.slice.value for n in ast.walk(ast.parse(C.CANARY_PROBE_SRC))
                  if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                  and n.value.id == "r" and isinstance(n.slice, ast.Constant)}
    runner_src = inspect.getsource(C.CanaryRunner)
    worker_keys = {item for item in SPEC_ITEMS if f'obs["{item}"]' in runner_src}
    assert probe_keys | worker_keys == set(SPEC_ITEMS)
    assert not probe_keys & worker_keys


def test_evaluate_compares_with_fixed_expectations():
    good = dict(C.EXPECTED)
    assert C.evaluate(good, "codex") == []
    bad = {**good, "listen_ipv6_eperm": "ok", "subreaper": "0"}
    assert C.evaluate(bad, "claude") == ["listen_ipv6_eperm", "subreaper"]
    assert C.evaluate({}, "codex") == list(SPEC_ITEMS)
    oc = {**good, **{k: "n/a" for k in C.PROXY_ITEMS}}
    assert C.evaluate(oc, "opencode") == []
    assert C.evaluate(good, "opencode") == list(C.PROXY_ITEMS)
    flipped = {**C.EXPECTED, "dev_tty": "ok"}
    assert C.evaluate(good, "codex", flipped) == ["dev_tty"]


def _open_fds() -> set[str]:
    import os

    return set(os.listdir("/proc/self/fd"))


@pytest.mark.parametrize("exc", ["refused", "unexpected"])
def test_canary_launch_failure_closes_the_pipe_and_listener(exc, tmp_path):
    """起動が拒否されても (想定外の例外でも)、pipe の両端と部分木の外の listener を閉じる。"""
    from types import SimpleNamespace

    from agentic_fx.core.improve_isolation import CliLaunchRefused

    class Launch:
        def start(self, argv, env, *, cwd, stdin):
            if exc == "refused":
                raise CliLaunchRefused("sandbox_unavailable: isolated_launch_once")
            raise OSError("injected")

    iso = SimpleNamespace(proxy=None, cgroup=SimpleNamespace(rel="/x/m-0"))
    runner = C.CanaryRunner(Launch(), iso, tmp_path)
    before = _open_fds()
    if exc == "refused":
        res = runner.run(None)
        assert (res.status, res.reason) == ("failed",
                                            "sandbox_unavailable: isolated_launch_once")
    else:
        with pytest.raises(OSError):
            runner.run(None)
    assert _open_fds() == before


def test_canary_hands_the_worker_a_fingerprint_over_its_python_artifact(tmp_path, monkeypatch):
    """canary の CLI 役は Python の ELF。worker は handshake の artifact で fingerprint を計算し
    直すので、handshake の fingerprint は admission の値の artifact の要素だけを Python の ELF に
    替えた値 (CLI の artifact が Python と別 file でも worker の再計算と一致する)。"""
    import os
    import shutil
    from types import SimpleNamespace

    from agentic_fx.config import load_settings
    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F
    from agentic_fx.core import improve_isolation as I
    from agentic_fx.runners import worker_runner as WR
    from agentic_fx.runners.base import MissionResult

    cli = tmp_path / "cli"
    shutil.copy2(os.path.realpath(sys.executable), cli)
    with open(cli, "ab") as f:
        f.write(b"\0" * 16)  # Python と内容も inode も違う CLI の artifact
    kernel = F.probe_kernel()
    ident = IA.inspect_artifact(str(cli))
    fp = F.compute_fingerprint("codex", ident, kernel)
    ctx = IA.AdmissionContext("codex", str(cli), ident, fp, SimpleNamespace(
        mission_allowed=lambda: True))
    seen: list[dict] = []

    class Capture:
        def __init__(self, **kw) -> None:
            seen.append(kw)

        def run(self, mission):
            return MissionResult("failed", None, [], reason="captured")

    monkeypatch.setattr(WR, "WorkerRunner", Capture)
    settings = load_settings(Path(__file__).resolve().parents[2] / "config"
                             / "settings.yaml.example")
    C.run_canary(ctx, settings=settings, root=tmp_path)
    assert len(seen) == 1
    spec = I.parse_isolation_field(seen[0]["isolation"])
    python = IA.inspect_artifact(os.path.realpath(sys.executable))
    assert spec.kind == "canary" and spec.artifact == python
    assert spec.fingerprint != fp.aggregate
    assert spec.fingerprint == F.compute_fingerprint("codex", python, kernel).aggregate
    assert I.verify_fingerprint(spec) == spec.fingerprint


# --- 実隔離の下 (使い捨て service) ------------------------------------------------------

@pytest.fixture
def isolation_units(tmp_path):
    from agentic_fx.core import landlock_nested
    from tests.core import _disposable_unit as du

    reason = du.environment_skip_reason()
    if reason is not None:
        pytest.skip(reason)
    if landlock_nested.probe_abi() < landlock_nested.MIN_ABI:
        pytest.skip("Landlock ABI < 8")
    units = du.DisposableUnits(tmp_path)
    try:
        yield units
    finally:
        units.cleanup()
    assert units.listed() == [], "使い捨て unit が残った"


def run_service(units, case: str, scenario: str, timeout: float = 170) -> dict:
    from tests.core import _disposable_unit as du

    out = units.tmp_path / f"{case}.json"
    command = [sys.executable, str(SERVICE_HELPER), scenario, str(out)]
    units.started.append(units.unit_name(case))
    r = du._run(units._argv(case, {"RuntimeMaxSec": "160"}, command, wait=True),
                timeout=timeout)
    if not out.exists():
        raise AssertionError(f"helper が結果を書かなかった (rc={r.returncode}): "
                             f"{r.stdout!r} {r.stderr!r}")
    res = json.loads(out.read_text())
    assert "error" not in res, res["error"]
    return res


@pytest.mark.parametrize("backend", ["codex", "claude", "opencode"])
def test_canary_runs_under_real_isolation(isolation_units, backend):
    res = run_service(isolation_units, f"canary-{backend}", f"canary:{backend}")
    m = res["mission"]
    assert m["status"] == "completed", m
    observed = m["output"]["canary"]
    expected = dict(C.EXPECTED)
    if backend == "opencode":
        expected.update({k: "n/a" for k in C.PROXY_ITEMS})
    assert observed == expected, observed
    assert m["output"]["error"] is None
    assert res["outcome"] == {"ok": True, "transient": False, "detail": ""}, res
    rep = m["report"]
    assert rep["kind"] == "canary" and rep["cleanup"]["reason"] is None
    assert rep["cleanup"]["steps"][:3] == ["supervisor_stop", "cgroup_kill", "populated_zero"]
    # canary の後も mission cgroup は片付き、gate は開いたまま
    assert res["m_dirs_left"] == [] and res["mission_allowed"] is True
    assert res["blocking_reasons"] == []


BACKENDS = ["codex", "claude", "opencode"]
RUNNER_CLASS = {"codex": "CodexRunner", "claude": "ClaudeRunner", "opencode": "OpencodeRunner"}
PATH_COMPONENTS = ("profile", "launch_kind", "outer_landlock", "argv_env_policy")

# どの scenario も CLI 役の artifact は共通の probe (Python の ELF) で、実 CLI は使わない (課金なし)。
# backend 固有であることは artifact ではなく、production の runner が組んだ argv / env / 設定 file と
# worker が計算し直した fingerprint の要素 (backend ごとに別の hash) で証明する。


def _expected_components(backend: str) -> dict:
    import sys

    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F

    fp = F.compute_fingerprint(backend, IA.inspect_artifact(sys.executable), F.probe_kernel())
    return {n: fp.component(n) for n in PATH_COMPONENTS}


def _assert_worker_observed(observed: dict, backend: str) -> None:
    """worker 役 (isolation を張った process) が観測した backend と fingerprint の要素が、backend
    ごとの期待 hash と一致する。"""
    assert observed["backend"] == backend
    assert observed["runner_class"] == RUNNER_CLASS[backend]
    assert observed["fp_backend"] == backend
    got = {n: observed["fp_components"][n] for n in PATH_COMPONENTS}
    assert got == _expected_components(backend)
    _assert_independent_digests(observed)


#: source を直接 hash する要素 (群 → 相対 path)。`SOURCE_GROUPS` を経ず、test が path を持つ
INDEPENDENT_SOURCES = {
    "source_launcher": ("core/cli_launcher.py", "core/cli_artifact_copy.py"),
    "source_supervisor": ("core/connect_supervisor.py",),
    "source_canary": ("core/improve_canary.py",),
}


def _assert_independent_digests(observed: dict) -> None:
    """worker が計算し直した要素のうち source の群と aggregate を、production の fingerprint module を
    通さずに hashlib だけで求めた値と照合する。

    独立に確かめられるのは source_* の bytes の digest と、要素の並びからの aggregate の計算。
    outer_landlock / argv_env_policy は AST の追跡で組む正規形で、test が同じ形を作り直すと
    production の写しになるので、上の `_expected_components` (production の関数) との照合に留まる。
    よって outer_landlock / argv_env_policy の canonicalization と production の
    `compute_fingerprint` が同時に壊れる変更は、ここでは検出できない。"""
    import hashlib

    from agentic_fx import core

    root = Path(core.__file__).resolve().parents[1]
    comps = observed["fp_components"]
    for name, rels in INDEPENDENT_SOURCES.items():
        lines = "".join(f"{rel}\0{hashlib.sha256((root / rel).read_bytes()).hexdigest()}\n"
                        for rel in rels)
        digest = hashlib.sha256(lines.encode()).hexdigest()
        # 要素の値は文字列 digest を JSON にしたもの (引用符つき) を hash する
        assert comps[name] == hashlib.sha256(f'"{digest}"'.encode()).hexdigest(), name
    joined = "".join(f"{n}\t{h}\n" for n, h in comps.items())
    assert observed["fp_aggregate"] == hashlib.sha256(joined.encode()).hexdigest()


def _assert_runner_built_cli_inputs(probe: dict, backend: str) -> None:
    """probe が受け取った argv / env / 設定 file は、その backend の runner が作ったもの。"""
    argv, env, files = probe["argv"], probe["env_values"], probe["config_files"]
    assert "PATH" in probe["env_keys"] and "PYTHONSAFEPATH" in probe["env_keys"]
    if backend == "codex":
        assert argv[:3] == ["exec", "-", "--json"]
        assert argv[argv.index("-m") + 1] == "probe"
        assert any(a.startswith("mcp_servers.afx.command=") for a in argv)
        assert "--ignore-user-config" in argv and "--output-schema" in argv
        assert env["CODEX_HOME"].endswith("/cfg") and "CLAUDE_CONFIG_DIR" not in env
        assert files["schema.json"] == {"type": "object", "required": ["probe"]}
    elif backend == "claude":
        assert argv[0] == "-p" and "--strict-mcp-config" in argv
        assert argv[argv.index("--model") + 1] == "probe"
        assert argv[argv.index("--mcp-config") + 1].endswith("/mcp.json")
        assert files["mcp.json"]["mcpServers"]["afx"]["args"][:2] == ["-m", "agentic_fx.tools.mcp_shim"]
        assert env["CLAUDE_CONFIG_DIR"].endswith("/cfg") and "CODEX_HOME" not in env
    else:
        assert argv[:2] == ["run", "隔離の結合の mission (CLI は probe)"]
        assert argv[argv.index("-m") + 1] == "llama-swap/probe"
        assert env["TERM"] == "dumb" and "CODEX_HOME" not in env
        assert "CLAUDE_CONFIG_DIR" not in env
        assert files["opencode.json"]["mcp"]["afx"]["enabled"] is True
        assert files["opencode.json"]["provider"]["llama-swap"]["models"]["probe"]


def test_backend_specific_fingerprint_components_differ():
    import sys

    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F

    ident = IA.inspect_artifact(sys.executable)
    kernel = F.probe_kernel()
    fps = {b: F.compute_fingerprint(b, ident, kernel) for b in BACKENDS}
    for name in ("argv_env_policy", "outer_landlock"):
        values = {b: fps[b].component(name) for b in BACKENDS}
        assert len(set(values.values())) == 3, (name, values)
    assert len({fp.aggregate for fp in fps.values()}) == 3


@pytest.mark.parametrize("backend", BACKENDS)
def test_worker_recomputes_and_rejects_a_different_fingerprint(isolation_units, backend):
    """afx が worker に渡す fingerprint が違えば、worker は自分で計算し直して fingerprint_mismatch で
    止まる (handshake の値をそのまま返す実装では止まらない)。CLI は起こさない。"""
    res = run_service(isolation_units, f"canary-mismatch-{backend}", f"canary_mismatch:{backend}")
    m = res["mission"]
    assert m["status"] != "completed", m
    assert "fingerprint_mismatch" in (m.get("reason") or ""), m
    assert res["outcome"]["ok"] is False, res
    assert m.get("output") is None or not m["output"].get("canary"), m
    assert res["m_dirs_left"] == [] and res["mission_allowed"] is True
    assert res["blocking_reasons"] == []


@pytest.mark.parametrize("backend", BACKENDS)
def test_canary_failure_blocks_exec_under_real_isolation(isolation_units, backend):
    from agentic_fx.core import improve_admission as IA
    from agentic_fx.core import improve_fingerprint as F

    res = run_service(isolation_units, f"canary-flip-{backend}", f"canary_blocks:{backend}")
    assert res["refusal"] == "safety_canary_failed"
    assert res["detail"] == "listen_ipv4_eperm"
    # canary を走らせた worker が観測した backend と、計算し直した fingerprint (backend ごとに別)
    rep = res["canary_report"]
    assert rep["kind"] == "canary" and rep["backend"] == backend
    ident = IA.inspect_artifact(__import__("sys").executable)
    expected = F.compute_fingerprint(backend, ident, F.probe_kernel()).aggregate
    assert rep["fingerprint_check"]["computed"] == expected
    assert rep["fingerprint_check"]["expected"] == expected
    # canary を通らなければ実 CLI の自己試験も mission も起こさない
    assert res["selftest_calls"] == 0 and res["options"] == []
    assert res["m_dirs_left"] == [] and res["mission_allowed"] is True


@pytest.mark.parametrize("kind", ["eagain", "503"])
def test_opencode_has_no_saturable_proxy(isolation_units, kind):
    """opencode は proxy を持たないので、飽和させる slot も tunnel も無い (宛先は直接の 8080 だけ)。

    proxy を持つ backend の負例 (飽和で admission が拒否・未登録) の代わりに、proxy が無いことを
    実機で確認する。opencode に proxy が付くと `proxy_port is None` / proxy env 空 / handoff 空の
    assert が赤になるので、その時点でこの test を proxy saturation の負例へ置換する。"""
    res = run_service(isolation_units, f"sat-{kind}-opencode", f"saturate:{kind}@opencode")
    m = res["mission"]
    assert m["result"]["status"] == "completed", m
    _assert_worker_observed(m["result"]["output"]["probe"]["observed"], "opencode")
    _assert_runner_built_cli_inputs(m["probe_observed"], "opencode")
    assert m["result"]["output"]["probe"]["env"] == {}
    assert m["report"]["proxy_port"] is None and m["report"]["handoff_verdicts"] == {}
    assert m["report"]["backend"] == "opencode"
    assert m["m_dirs_left"] == []


@pytest.mark.parametrize("backend", ["codex", "claude"])
@pytest.mark.parametrize("kind", ["eagain", "503"])
def test_saturation_and_503_do_not_register(isolation_units, kind, backend):
    res = run_service(isolation_units, f"sat-{kind}-{backend}", f"saturate:{kind}@{backend}")
    m = res["mission"]
    # 失敗した mission でも、起動時に probe が受け取ったものは残る
    _assert_worker_observed(m["probe_observed"]["observed"], backend)
    _assert_runner_built_cli_inputs(m["probe_observed"], backend)
    assert m["report"]["backend"] == backend
    probe = res["mission"]["result"]
    # probe は EAGAIN / 503 を見たときだけ 7 で終わる (実 CLI と同じく要求の失敗で異常終了)
    assert probe["status"] == "failed" and "rc=7" in probe["reason"], probe
    assert res["first"]["refusal"] == "runtime_fingerprint_selftest_failed"
    # 失敗は許可として覚えない・対応集合に入らない (2 回目は同じ失敗の判定を返す)
    assert res["second"] == {"refusal": "runtime_fingerprint_selftest_failed", "same": True}
    assert res["selftest_runs"] == 1
    assert res["supported"] == []
    assert res["mission"]["m_dirs_left"] == []
