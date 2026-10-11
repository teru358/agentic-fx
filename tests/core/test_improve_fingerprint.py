"""改善 mission の CLI 隔離の fingerprint (要素の網羅、artifact の hash、空の対応集合)。"""
from __future__ import annotations

import dataclasses
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

from agentic_fx.core import improve_fingerprint as F

PY = os.path.realpath(sys.executable)

#: 仕様 §8 の列挙 (順序も固定)
SPEC_COMPONENTS = (
    "profile", "seccomp_policy", "bpf", "nested_landlock", "outer_landlock", "egress",
    "source_launcher", "source_protocol", "source_supervisor", "source_proxy", "source_spool",
    "source_cgroup", "source_canary", "source_isolation", "kernel", "userspace", "artifact", "host_artifact",
    "package_manifest", "launch_kind",
    "argv_env_policy",
)

KERNEL = F.KernelCapabilities(machine="x86_64", landlock_abi=8, seccomp_missing=(),
                              supervisor_missing=(), yama="1", inet_diag="ok")
USERSPACE = {"kernel_release": "7.0.0", "machine": "x86_64", "python_version": "3.12.0"}


def _artifact(tmp_path: Path) -> F.ArtifactIdentity:
    p = tmp_path / "artifact.bin"
    p.write_bytes(b"\x7fELF" + b"\0" * 60)
    fd = os.open(p, os.O_RDONLY | os.O_CLOEXEC)
    try:
        return F.hash_artifact_fd(fd)
    finally:
        os.close(fd)


def _src_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "src" / "agentic_fx"
    shutil.copytree(F.SOURCE_ROOT, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return dst


def _flip_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    data[-1] ^= 0x01
    path.write_bytes(bytes(data))


def test_components_match_spec_enumeration():
    assert F.COMPONENTS == SPEC_COMPONENTS
    assert set(F.SOURCE_GROUPS) == {"launcher", "protocol", "supervisor", "proxy", "spool",
                                    "cgroup", "canary", "isolation"}
    # proxy は inet_diag / handoff を含む egress の全 module
    files = {p.name for p in F._group_files(F.SOURCE_ROOT, F.SOURCE_GROUPS["proxy"])}
    assert {"proxy.py", "handoff.py", "inet_diag.py", "resolver.py", "relay.py"} <= files


# 1 file の 1 byte を変える要素 (source の一時 copy)
SOURCE_MUTATIONS = {
    "source_launcher": "core/cli_launcher.py",
    "source_protocol": "core/cli_launch.py",
    "source_supervisor": "core/connect_supervisor.py",
    "source_proxy": "egress/inet_diag.py",
    "source_spool": "tools/mcp_shim.py",
    "source_cgroup": "core/cgroup_host.py",
    "source_canary": "loops/verify_backend.py",
    "source_isolation": "core/improve_isolation.py",
    "outer_landlock": "core/landlock.py",
    "argv_env_policy": "runners/codex_runner.py",
}


@pytest.mark.parametrize("name", SPEC_COMPONENTS)
def test_fingerprint_components_are_complete(name, tmp_path, monkeypatch):
    """各要素を 1 つだけ変えると、その要素の hash と aggregate が変わり、他の要素は変わらない。"""
    root = _src_copy(tmp_path)
    art = _artifact(tmp_path)
    base = F.compute_fingerprint("codex", art, KERNEL, root=root, userspace=USERSPACE)
    kernel, userspace, backend = KERNEL, dict(USERSPACE), "codex"
    if name in SOURCE_MUTATIONS:
        _flip_byte(root / SOURCE_MUTATIONS[name])
    elif name == "kernel":
        kernel = dataclasses.replace(KERNEL, yama="0")
    elif name == "userspace":
        userspace["python_version"] = "3.12.1"
    elif name == "artifact":
        art = dataclasses.replace(art, mtime_ns=art.mtime_ns + 1)
    elif name == "launch_kind":
        monkeypatch.setattr(F, "LAUNCH_KIND_PATH_FD", "script")
    elif name in ("host_artifact", "package_manifest"):
        pytest.skip("codex package fixture is covered by admission")
    elif name == "profile":
        from agentic_fx.core import seccomp_cli_policy
        monkeypatch.setattr(seccomp_cli_policy, "PROFILE_ID", "improve-cli/2")
    elif name in ("seccomp_policy", "bpf"):
        from agentic_fx.core import seccomp_cli_bpf
        real = seccomp_cli_bpf.compile_policy()
        changed = (dataclasses.replace(real, policy_hash="0" * 64) if name == "seccomp_policy"
                   else dataclasses.replace(real, filter_flags=real.filter_flags | 2))
        monkeypatch.setattr(seccomp_cli_bpf, "compile_policy", lambda: changed)
    elif name == "nested_landlock":
        from agentic_fx.core import landlock_nested
        monkeypatch.setattr(landlock_nested, "NESTED_RULESET", dataclasses.replace(
            landlock_nested.NESTED_RULESET, handled_scope=1))
    elif name == "egress":
        from agentic_fx.egress import policy
        monkeypatch.setattr(policy, "EGRESS_HOSTS", {**policy.EGRESS_HOSTS,
                                                     "claude": frozenset({"evil.example"})})
    else:  # pragma: no cover
        raise AssertionError(name)
    after = F.compute_fingerprint(backend, art, kernel, root=root, userspace=userspace)
    changed = [n for (n, h), (_, h2) in zip(base.components, after.components) if h != h2]
    assert changed == [name]
    assert after.aggregate != base.aggregate


def test_verify_backend_source_changes_canary_component_and_aggregate(tmp_path):
    """自己試験の成功条件を変える backend は canary の再判定を強制する。"""
    root = _src_copy(tmp_path)
    art = _artifact(tmp_path)
    base = F.compute_fingerprint("codex", art, KERNEL, root=root, userspace=USERSPACE)

    _flip_byte(root / "loops" / "verify_backend.py")

    after = F.compute_fingerprint("codex", art, KERNEL, root=root, userspace=USERSPACE)
    changed = [name for (name, digest), (_, later) in zip(base.components, after.components)
               if digest != later]
    assert changed == ["source_canary"]
    assert after.aggregate != base.aggregate


def test_outer_landlock_changes_with_backend_proc_rule():
    claude = F.outer_landlock_form("claude")
    codex = F.outer_landlock_form("codex")
    assert "/proc" in claude["read_only"] and "/proc" not in codex["read_only"]


def test_artifact_mutation_changes_fingerprint(tmp_path):
    p = tmp_path / "cli"
    p.write_bytes(b"\x7fELF" + b"A" * 100)

    def ident():
        fd = os.open(p, os.O_RDONLY | os.O_CLOEXEC)
        try:
            return F.hash_artifact_fd(fd)
        finally:
            os.close(fd)

    first = ident()
    fp1 = F.compute_fingerprint("codex", first, KERNEL, userspace=USERSPACE)
    # 内容の 1 byte (mtime は戻す)
    st = os.stat(p)
    _flip_byte(p)
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    second = ident()
    assert second.sha256 != first.sha256 and second.mtime_ns == first.mtime_ns
    fp2 = F.compute_fingerprint("codex", second, KERNEL, userspace=USERSPACE)
    assert fp2.aggregate != fp1.aggregate
    # mtime だけ
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000))
    third = ident()
    assert third.sha256 == second.sha256 and third.mtime_ns != second.mtime_ns
    assert F.compute_fingerprint("codex", third, KERNEL,
                                 userspace=USERSPACE).aggregate != fp2.aggregate


def test_codex_package_artifacts_are_fingerprint_components(tmp_path):
    package = tmp_path / "package"
    bindir = package / "bin"
    bindir.mkdir(parents=True)
    binary = bindir / "codex"
    host = bindir / "codex-code-mode-host"
    binary.write_bytes(b"codex")
    host.write_bytes(b"host-a")
    manifest = package / "codex-package.json"
    manifest.write_bytes(b'{"layoutVersion":1}')
    fd = os.open(binary, os.O_RDONLY | os.O_CLOEXEC)
    try:
        artifact = F.hash_artifact_fd(fd)
    finally:
        os.close(fd)
    before = F.component_values("codex", artifact, KERNEL, userspace=USERSPACE,
                                artifact_path=str(binary))
    host.write_bytes(b"host-b")
    after_host = F.component_values("codex", artifact, KERNEL, userspace=USERSPACE,
                                    artifact_path=str(binary))
    manifest.write_bytes(b'{"layoutVersion":2}')
    after_manifest = F.component_values("codex", artifact, KERNEL, userspace=USERSPACE,
                                        artifact_path=str(binary))
    assert before["launch_kind"] == F.LAUNCH_KIND_PATH_FD
    assert before["host_artifact"] != after_host["host_artifact"]
    assert after_host["package_manifest"] != after_manifest["package_manifest"]


def test_hash_reads_the_fd_not_the_path(tmp_path):
    """hash は開いた fd の内容。path を別 file に rename で差し替えても hash は元の inode。"""
    p = tmp_path / "cli"
    p.write_bytes(b"original")
    fd = os.open(p, os.O_RDONLY | os.O_CLOEXEC)
    try:
        os.lseek(fd, 3, os.SEEK_SET)
        other = tmp_path / "other"
        other.write_bytes(b"replacement")
        os.rename(other, p)
        ident = F.hash_artifact_fd(fd)
        # offset は動かさない (launcher に渡す fd の状態を変えない)
        assert os.lseek(fd, 0, os.SEEK_CUR) == 3
    finally:
        os.close(fd)
    import hashlib
    assert ident.sha256 == hashlib.sha256(b"original").hexdigest()
    assert ident.ino != os.stat(p).st_ino


def test_artifact_identity_round_trip_and_validation():
    ident = F.ArtifactIdentity("a" * 64, 1, 2, 3, 4)
    assert F.ArtifactIdentity.from_dict(ident.as_dict()) == ident
    for bad in ({**ident.as_dict(), "sha256": "A" * 64}, {**ident.as_dict(), "ino": -1},
                {**ident.as_dict(), "extra": 1}, {**ident.as_dict(), "size": True}, "x"):
        with pytest.raises(ValueError):
            F.ArtifactIdentity.from_dict(bad)


def test_supported_set_is_a_literal_of_registered_aggregates():
    assert type(F.SUPPORTED_FINGERPRINTS) is frozenset
    assert all(re.fullmatch(r"[0-9a-f]{64}", a) for a in F.SUPPORTED_FINGERPRINTS)
    assert len(F.SUPPORTED_FINGERPRINTS) == 3
    # 公開 API に集合へ足す経路が無い (名前を列挙して固定する)
    assert sorted(F.__all__) == sorted([
        "BACKENDS", "COMPONENTS", "EXCLUDED_ENV_PREFIXES", "LAUNCH_KIND_NATIVE_ELF", "LAUNCH_KIND_PATH_FD",
        "NO_PROXY_ENV_KEYS", "NO_PROXY_VALUE", "PROXY_ENV_KEYS", "SOURCE_GROUPS",
        "SOURCE_ROOT", "SUPPORTED_FINGERPRINTS", "ArtifactChanged", "ArtifactIdentity",
        "ImproveFingerprint", "KernelCapabilities", "argv_env_policy_form",
        "component_values", "compute_fingerprint", "env_policy", "fingerprint_from_values",
        "hash_artifact_fd", "is_supported", "isolated_cli_env", "outer_landlock_form",
        "outer_landlock_sources", "probe_kernel", "source_digest", "userspace_identity",
        "with_artifact"])
    public = {n for n in dir(F) if not n.startswith("_")}
    assert not {n for n in public if any(w in n.lower() for w in
                                          ("register", "add_", "supported_add", "enroll"))}
    fp = F.ImproveFingerprint((), "f" * 64)
    assert F.is_supported(fp) is False


def test_isolated_env_policy():
    base = {"PATH": "/usr/bin:/bin", "HOME": "/h", "CODEX_MANAGED_BY_NPM": "1",
            "HTTPS_PROXY": "http://evil:1"}
    codex = F.isolated_cli_env("codex", base, 41234)
    assert codex["HTTPS_PROXY"] == codex["https_proxy"] == "http://127.0.0.1:41234"
    assert codex["HTTP_PROXY"] == codex["http_proxy"] == "http://127.0.0.1:41234"
    assert codex["NO_PROXY"] == "localhost,127.0.0.1"
    assert "CODEX_MANAGED_BY_NPM" not in codex and codex["PATH"] == "/usr/bin:/bin"
    oc = F.isolated_cli_env("opencode", base, None)
    assert not {k for k in oc if "proxy" in k.lower()}
    with pytest.raises(ValueError):
        F.isolated_cli_env("claude", base, None)


def test_probe_kernel_on_this_host():
    k = F.probe_kernel()
    assert k.machine == os.uname().machine
    assert k.landlock_abi >= 1


def test_userspace_identity_records_python_build(monkeypatch):
    monkeypatch.setattr(F.platform, "python_build", lambda: ("build-a", "date-a"))
    first = F.userspace_identity()["python_build"]
    monkeypatch.setattr(F.platform, "python_build", lambda: ("build-b", "date-b"))
    assert first == ["build-a", "date-a"]
    assert F.userspace_identity()["python_build"] == ["build-b", "date-b"]


def test_aggregate_hashes_components_in_spec_order():
    """aggregate は仕様の列挙順に `名前 \\t hash \\n` を並べた hash (名前順ではない)。"""
    values = {n: f"v-{n}" for n in SPEC_COMPONENTS}
    fp = F.fingerprint_from_values(values)
    lines = "".join(f"{n}\t{F._sha(F._canon(values[n]))}\n" for n in SPEC_COMPONENTS)
    assert fp.aggregate == F._sha(lines.encode())


def test_function_source_hash_covers_the_whole_body(tmp_path):
    p = tmp_path / "m.py"
    p.write_text("def f():\n    a = 1\n    return a\n", encoding="utf-8")
    first = F._function_sources(p, ("f",))["f"]
    p.write_text("def f():\n    a = 1\n    return a + 1\n", encoding="utf-8")
    assert F._function_sources(p, ("f",))["f"] != first


#: outer Landlock の規則を決める source (bootstrap から参照を辿って得る集合)。bootstrap が
#: 新しい関数・定数を参照し始めたらこの表が合わなくなり、ここで落ちる
OUTER_LANDLOCK_SOURCES = {
    "core/landlock.py",
    "mission_worker.py:_bootstrap_improve_profile",
    "mission_worker.py:_guarded_data_dir",
    "mission_worker.py:_exec_closure_for",
    "mission_worker.py:ExecClosure",
    "runners/cli_runner.py:_TRANSCRIPT_DIR_DEFAULT",
}


def test_outer_landlock_sources_follow_the_bootstrap():
    assert set(F.outer_landlock_sources()) == OUTER_LANDLOCK_SOURCES
    assert set(F.outer_landlock_form("codex")["sources"]) == OUTER_LANDLOCK_SOURCES


def _segment_of(path: Path, name: str) -> str:
    import ast as _ast
    text = path.read_text(encoding="utf-8")
    for node in _ast.parse(text).body:
        names = ([node.name] if hasattr(node, "name")
                 else [t.id for t in getattr(node, "targets", [getattr(node, "target", None)])
                       if isinstance(t, _ast.Name)])
        if name in names:
            return _ast.get_source_segment(text, node)
    raise AssertionError(name)


@pytest.mark.parametrize("key", sorted(OUTER_LANDLOCK_SOURCES))
def test_outer_landlock_changes_with_each_source(key, tmp_path):
    """役割表の各 source の中の 1 箇所を変えると outer_landlock と aggregate が変わる。"""
    root = _src_copy(tmp_path)
    art = _artifact(tmp_path)
    base = F.compute_fingerprint("codex", art, KERNEL, root=root, userspace=USERSPACE)
    rel, _, name = key.partition(":")
    path = root / rel
    text = path.read_text(encoding="utf-8")
    if name:
        seg = _segment_of(path, name)
        assert text.count(seg) == 1
        # 区間の 2 行目の前に注釈行を 1 つ足す (区間の中だけを変える)
        changed = seg.replace("\n", "\n#\n", 1)
        path.write_text(text.replace(seg, changed), encoding="utf-8")
        assert _segment_of(path, name) == changed
    else:
        _flip_byte(path)
    after = F.compute_fingerprint("codex", art, KERNEL, root=root, userspace=USERSPACE)
    changed_components = [n for (n, h), (_, h2) in zip(base.components, after.components)
                          if h != h2]
    assert "outer_landlock" in changed_components
    assert after.aggregate != base.aggregate


def test_outer_landlock_sources_pick_up_a_new_helper(tmp_path):
    """bootstrap が新しい関数を呼べば、その関数も hash の対象に入る (表を手で足さなくてよい)。"""
    root = _src_copy(tmp_path)
    path = root / "mission_worker.py"
    text = path.read_text(encoding="utf-8")
    anchor = "    code_root = Path(__file__).resolve().parents[1]\n"
    assert text.count(anchor) == 1
    text = text.replace(anchor, anchor + "    _extra_rule()\n")
    text += "\n\ndef _extra_rule() -> None:\n    return None\n"
    path.write_text(text, encoding="utf-8")
    keys = set(F.outer_landlock_sources(root))
    assert keys == OUTER_LANDLOCK_SOURCES | {"mission_worker.py:_extra_rule"}


#: 隔離の組み立ての部品 (file と、その中の定義)。どれか 1 箇所を変えると source_isolation が変わる
ISOLATION_SITES = (
    ("core/improve_isolation.py", "StdinRouter"),
    ("core/improve_isolation.py", "WorkerIsolation"),
    ("core/improve_isolation.py", "IsolatedLaunch"),
    ("core/improve_admission.py", "ImproveAdmission"),
    ("core/improve_admission.py", "choose_for_mission"),
    ("core/improve_admission.py", "preflight_reason"),
    ("mission_worker.py", "main"),
    ("mission_worker.py", "_finish_isolation"),
)


@pytest.mark.parametrize("rel,name", ISOLATION_SITES)
def test_isolation_assembly_changes_the_fingerprint(rel, name, tmp_path):
    root = _src_copy(tmp_path)
    art = _artifact(tmp_path)
    base = F.compute_fingerprint("codex", art, KERNEL, root=root, userspace=USERSPACE)
    path = root / rel
    text = path.read_text(encoding="utf-8")
    seg = _segment_of(path, name)
    assert text.count(seg) == 1
    if "\n" in seg:
        changed = seg.replace("\n", "\n#\n", 1)
    else:
        # 1 行の定数は式を変える (行末の注釈は区間に入らない)
        changed = seg + " | 0"
    path.write_text(text.replace(seg, changed), encoding="utf-8")
    after = F.compute_fingerprint("codex", art, KERNEL, root=root, userspace=USERSPACE)
    changed_components = [n for (n, h), (_, h2) in zip(base.components, after.components)
                          if h != h2]
    assert "source_isolation" in changed_components
    assert after.aggregate != base.aggregate


def test_fingerprint_module_is_not_hashed(tmp_path):
    """対応済み集合を足す変更 (この module の定数) は source_isolation を変えない。"""
    root = _src_copy(tmp_path)
    before = F.source_digest("isolation", root)
    path = root / "core" / "improve_fingerprint.py"
    text = path.read_text(encoding="utf-8")
    replaced, n = re.subn(r"SUPPORTED_FINGERPRINTS: frozenset\[str\] = frozenset\(\{.*?\}\)",
                          'SUPPORTED_FINGERPRINTS: frozenset[str] = frozenset({"a"})',
                          text, count=1, flags=re.DOTALL)
    assert n == 1 and replaced != text
    path.write_text(replaced, encoding="utf-8")
    assert F.source_digest("isolation", root) == before


#: memfd の封印 (launcher が mission cgroup の中で写す部品)。変えると source_launcher だけが変わる
ARTIFACT_COPY_SITES = ("sealed_copy", "memfd_create", "exec_memfd", "sha256_fd",
                       "inspect_cli_artifact", "EXEC_COPY_SEALS", "SYS_MEMFD_CREATE")


@pytest.mark.parametrize("name", ARTIFACT_COPY_SITES)
def test_artifact_copy_changes_the_launcher_source(name, tmp_path):
    root = _src_copy(tmp_path)
    art = _artifact(tmp_path)
    base = F.compute_fingerprint("codex", art, KERNEL, root=root, userspace=USERSPACE)
    path = root / "core" / "cli_artifact_copy.py"
    text = path.read_text(encoding="utf-8")
    seg = _segment_of(path, name)
    assert text.count(seg) == 1
    changed = seg.replace("\n", "\n#\n", 1) if "\n" in seg else seg + " or 0"
    path.write_text(text.replace(seg, changed), encoding="utf-8")
    after = F.compute_fingerprint("codex", art, KERNEL, root=root, userspace=USERSPACE)
    changed_components = [n for (n, h), (_, h2) in zip(base.components, after.components)
                          if h != h2]
    assert changed_components == ["source_launcher"]


def test_with_artifact_replaces_only_the_artifact_component():
    """canary の handshake の fingerprint は artifact の要素だけを差し替えた値で、worker が
    その artifact で計算し直した値と一致する。"""
    a = F.ArtifactIdentity("a" * 64, 1, 2, 3, 4)
    b = F.ArtifactIdentity("b" * 64, 5, 6, 7, 8)
    kernel = F.KernelCapabilities(machine="x86_64", landlock_abi=8, seccomp_missing=(),
                                  supervisor_missing=(), yama="1", inet_diag="ok")
    fa = F.compute_fingerprint("codex", a, kernel)
    fb = F.compute_fingerprint("codex", b, kernel)
    swapped = F.with_artifact(fa, b)
    assert swapped == fb and swapped.aggregate != fa.aggregate
    assert [n for (n, h), (_, g) in zip(fa.components, swapped.components) if h != g] == [
        "artifact"]
