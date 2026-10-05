"""runtime fingerprint、対応集合の判定、集合外の代表自己試験と process 内 cache。

代表自己試験は使い捨ての子 process の中で filter を掛ける。pytest の process には掛けない。
"""
from __future__ import annotations

import dataclasses
import hashlib
import os
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from agentic_fx.core import runtime_fingerprint as F
from agentic_fx.core import seccomp as S

SPEC_NUMPY_RECORD = "1302287028025a50047b0a8ca45e78195209fdc9a53efb0d25c3a7a86a2e792b"
SPEC_PANDAS_RECORD = "e9ba5610142346491f808af3ed84b371105e227ddb43d5b761bff583c2c990f3"


def _write_dist(root: Path, name: str, version: str, record: bytes,
                tags=("cp313-cp313-manylinux_2_28_x86_64",), generator="meson") -> Path:
    d = root / f"{name}-{version}.dist-info"
    d.mkdir(parents=True)
    (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    wheel = "Wheel-Version: 1.0\nGenerator: " + generator + "\nRoot-Is-Purelib: false\n"
    wheel += "".join(f"Tag: {t}\n" for t in tags)
    (d / "WHEEL").write_text(wheel)
    (d / "RECORD").write_bytes(record)
    return d


# --- fingerprint の構成 --------------------------------------------------------

def test_dist_identity_reads_version_generator_tags_and_record_hash(tmp_path):
    record = b"numpy/__init__.py,sha256=abc,123\nnumpy-2.5.1.dist-info/RECORD,,\n"
    d = _write_dist(tmp_path, "numpy", "2.5.1", record,
                    tags=("cp313-cp313-manylinux_2_28_x86_64", "cp313-cp313-manylinux_2_27_x86_64"))
    ident = F.dist_identity(d)
    assert ident == F.DistIdentity(
        name="numpy", version="2.5.1", generator="meson",
        tags=("cp313-cp313-manylinux_2_27_x86_64", "cp313-cp313-manylinux_2_28_x86_64"),
        record_sha256=hashlib.sha256(record).hexdigest())


@pytest.mark.parametrize("which", ["numpy", "pandas"])
def test_one_bit_in_record_makes_a_different_fingerprint(tmp_path, which):
    base = F.supported_runtimes()[0]
    record = b"pkg/x.py,sha256=AAAA,10\n" * 50
    flipped = bytearray(record)
    flipped[123] ^= 0x01
    a = F.dist_identity(_write_dist(tmp_path / "a", which, "1.0", record))
    b = F.dist_identity(_write_dist(tmp_path / "b", which, "1.0", bytes(flipped)))
    assert (a.version, a.generator, a.tags) == (b.version, b.generator, b.tags)
    assert a.record_sha256 != b.record_sha256
    fa = dataclasses.replace(base, **{which: a})
    fb = dataclasses.replace(base, **{which: b})
    assert fa != fb
    assert fa.normalized() != fb.normalized()
    assert hash(fa) != hash(fb) or fa != fb


def test_supported_set_is_the_single_spec_entry_with_both_record_hashes():
    runtimes = F.supported_runtimes()
    assert len(runtimes) == 1
    s = runtimes[0]
    assert (s.kernel_release, s.machine, s.landlock_abi) == ("7.0.0-38-generic", "x86_64", 8)
    assert (s.glibc_version, s.glibc_build) == ("2.43", "Ubuntu GLIBC 2.43-2ubuntu2.4")
    assert (s.python_version, s.python_build) == ("3.13.14", ("main", "Jun 11 2026 04:03:13"))
    assert s.numpy == F.DistIdentity("numpy", "2.5.1", "meson",
                                     ("cp313-cp313-manylinux_2_27_x86_64",
                                      "cp313-cp313-manylinux_2_28_x86_64"), SPEC_NUMPY_RECORD)
    assert s.pandas == F.DistIdentity("pandas", "3.0.5", "meson",
                                      ("cp313-cp313-manylinux_2_24_x86_64",
                                       "cp313-cp313-manylinux_2_28_x86_64"), SPEC_PANDAS_RECORD)
    assert (s.sandbox_profile, s.seccomp_profile) == ("plugin-worker/6", "allow/6")
    norm = s.normalized()
    assert SPEC_NUMPY_RECORD in norm and SPEC_PANDAS_RECORD in norm


def test_normalized_is_canonical_and_contains_every_field():
    s = F.supported_runtimes()[0]
    assert s.normalized() == dataclasses.replace(s).normalized()
    for field in dataclasses.fields(F.RuntimeFingerprint):
        assert f'"{field.name}"' in s.normalized()


def _variants(base: F.RuntimeFingerprint):
    np_, pd_ = base.numpy, base.pandas
    yield "kernel_release", dataclasses.replace(base, kernel_release="7.0.0-39-generic")
    yield "machine", dataclasses.replace(base, machine="aarch64")
    for abi in (3, 4, 5, 6, 7, 9):
        yield f"landlock_abi={abi}", dataclasses.replace(base, landlock_abi=abi)
    yield "glibc_version", dataclasses.replace(base, glibc_version="2.42")
    yield "glibc_build", dataclasses.replace(base, glibc_build="Ubuntu GLIBC 2.43-2ubuntu2.5")
    yield "python_version", dataclasses.replace(base, python_version="3.13.15")
    yield "python_build", dataclasses.replace(base, python_build=("main", "Jul 1 2026 00:00:00"))
    yield "sandbox_profile", dataclasses.replace(base, sandbox_profile="plugin-worker/7")
    yield "seccomp_profile", dataclasses.replace(base, seccomp_profile="allow/7")
    for which, ident in (("numpy", np_), ("pandas", pd_)):
        yield f"{which}.version", dataclasses.replace(
            base, **{which: dataclasses.replace(ident, version="9.9.9")})
        yield f"{which}.generator", dataclasses.replace(
            base, **{which: dataclasses.replace(ident, generator="setuptools")})
        yield f"{which}.tags", dataclasses.replace(
            base, **{which: dataclasses.replace(ident, tags=ident.tags[:1])})
        yield f"{which}.record", dataclasses.replace(
            base, **{which: dataclasses.replace(ident, record_sha256="0" * 64)})


def test_is_supported_matches_only_the_pinned_entry():
    base = F.supported_runtimes()[0]
    assert F.is_supported(base)
    for name, variant in _variants(base):
        assert not F.is_supported(variant), name


def test_executable_identity_is_in_the_fingerprint_but_not_pinned():
    base = F.supported_runtimes()[0]
    other = dataclasses.replace(base, python_executable="/opt/other/python3.13")
    assert other != base
    assert F.is_supported(other)


def test_compute_fingerprint_on_this_host_takes_landlock_abi_from_the_caller():
    fp8 = F.compute_fingerprint(landlock_abi=8)
    fp5 = F.compute_fingerprint(landlock_abi=5)
    assert (fp8.landlock_abi, fp5.landlock_abi) == (8, 5)
    assert dataclasses.replace(fp5, landlock_abi=8) == fp8
    assert fp8.sandbox_profile == "plugin-worker/6"
    assert fp8.seccomp_profile == S.SECCOMP_PROFILE
    assert fp8.numpy.name == "numpy" and fp8.pandas.name == "pandas"
    assert len(fp8.numpy.record_sha256) == 64
    assert os.path.isabs(fp8.python_executable)
    assert not F.is_supported(fp5)


def test_compute_fingerprint_reads_record_bytes_of_the_installed_dists():
    import importlib.metadata as md
    fp = F.compute_fingerprint(landlock_abi=8)
    for which in ("numpy", "pandas"):
        dist = md.distribution(which)
        rec = next(f for f in dist.files if str(f).endswith(".dist-info/RECORD"))
        raw = Path(dist.locate_file(rec)).read_bytes()
        assert getattr(fp, which).record_sha256 == F.normalized_record_sha256(raw)
        assert getattr(fp, which).version == dist.version


def test_this_host_fingerprint_is_in_the_supported_set():
    fp = F.compute_fingerprint(landlock_abi=8)
    s = F.supported_runtimes()[0]
    if (fp.kernel_release, fp.machine) != (s.kernel_release, s.machine):
        pytest.skip("not the measured host")
    assert dataclasses.replace(fp, python_executable=s.python_executable) == s
    assert F.is_supported(fp)


def test_normalized_record_drops_lines_outside_site_packages_only():
    record = (b"../../../bin/f2py,sha256=AAAA,333\n"
              b"numpy/__init__.py,sha256=BBBB,10\n"
              b"../../../bin/numpy-config,sha256=CCCC,333\r\n"
              b"numpy-2.5.1.dist-info/RECORD,,\n")
    assert F.normalized_record_sha256(record) == hashlib.sha256(
        b"numpy/__init__.py,sha256=BBBB,10\nnumpy-2.5.1.dist-info/RECORD,,\n").hexdigest()


def test_bit_flip_in_outside_lines_does_not_change_fingerprint_but_inside_does(tmp_path):
    head = b"../../../bin/f2py,sha256=AAAA,333\n"
    body = b"numpy/x.py,sha256=BBBB,10\n" * 20
    record = head + body
    flip_head = bytearray(record)
    flip_head[20] ^= 0x01
    flip_body = bytearray(record)
    flip_body[len(head) + 30] ^= 0x01
    a = F.dist_identity(_write_dist(tmp_path / "a", "numpy", "1.0", record))
    b = F.dist_identity(_write_dist(tmp_path / "b", "numpy", "1.0", bytes(flip_head)))
    c = F.dist_identity(_write_dist(tmp_path / "c", "numpy", "1.0", bytes(flip_body)))
    assert a == b
    assert a.record_sha256 != c.record_sha256
    base = F.supported_runtimes()[0]
    assert dataclasses.replace(base, numpy=a) == dataclasses.replace(base, numpy=b)
    assert dataclasses.replace(base, numpy=a) != dataclasses.replace(base, numpy=c)


def test_normalized_record_keeps_lines_with_dotdot_in_the_middle():
    kept = b"numpy/a/../b.py,sha256=AAAA,10\nnumpy/c.py,sha256=BBBB,2\n"
    record = b"../../../bin/f2py,sha256=XXXX,1\n" + kept
    assert F.normalized_record_sha256(record) == hashlib.sha256(kept).hexdigest()


# --- 代表計算の oracle ----------------------------------------------------------

def test_oracle_and_workload_agree_without_isolation():
    got = F.representative_workload()
    assert F.compare_with_oracle(got) is None


def test_compare_with_oracle_names_the_first_mismatch():
    got = F.representative_workload()
    bad = dict(got)
    bad["rsi_last"] = got["rsi_last"] + 1e-6
    assert F.compare_with_oracle(bad) == "oracle_mismatch:rsi_last"
    missing = {k: v for k, v in got.items() if k != "tz_offsets"}
    assert F.compare_with_oracle(missing) == "oracle_mismatch:tz_offsets"
    extra = dict(got, surprise=1)
    assert F.compare_with_oracle(extra) == "oracle_mismatch:surprise"


# --- 代表自己試験 (実子 process) --------------------------------------------------

_HOOKS_DIR = str(Path(__file__).resolve().parent)
_FULL = "selftest_isolation_hooks:isolate"


def _env(extra: Path | None = None) -> dict:
    return {"PYTHONPATH": os.pathsep.join([_HOOKS_DIR] + ([str(extra)] if extra else []))}


def test_selftest_passes_under_landlock_and_seccomp():
    out = F.run_representative_selftest(isolation_hook=_FULL, env=_env())
    assert out == F.SelftestOutcome(ok=True, detail="ok")


@pytest.mark.parametrize("name", ["seccomp_only", "forged", "stale_profile", "low_abi"])
def test_selftest_rejects_incomplete_or_forged_isolation(name):
    out = F.run_representative_selftest(isolation_hook=f"selftest_isolation_hooks:{name}",
                                        env=_env())
    assert out == F.SelftestOutcome(ok=False, detail="isolation_not_applied")


@pytest.mark.parametrize("bad", [None, ""])
def test_selftest_requires_an_isolation_hook(bad):
    with pytest.raises(ValueError):
        F.run_representative_selftest(isolation_hook=bad)


def test_selftest_without_hook_argument_is_a_type_error():
    with pytest.raises(TypeError):
        F.run_representative_selftest()


def _hook_module(tmp_path: Path, body: str) -> dict:
    (tmp_path / "afx_selftest_hook.py").write_text(textwrap.dedent(body))
    return _env(tmp_path)


def test_selftest_fails_when_the_isolation_hook_does_not_apply_seccomp(tmp_path):
    env = _hook_module(tmp_path, """
        def apply():
            return None
    """)
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env)
    assert out == F.SelftestOutcome(ok=False, detail="isolation_not_applied")


def test_selftest_fails_with_sigsys_when_isolation_kills_threads(tmp_path):
    env = _hook_module(tmp_path, """
        import dataclasses, os
        from agentic_fx.core import landlock, runtime_fingerprint as F, seccomp as S
        def apply():
            applied = landlock.apply_plugin_ruleset([])
            table = dataclasses.replace(S.ALLOW6_TABLE, fallback_enosys=())
            S.install_program(S.build_program(table, pid=os.getpid()))
            return {**applied.attestation_fields(), "seccomp_profile": S.SECCOMP_PROFILE,
                    "sandbox_profile": F.SANDBOX_PROFILE_VERSION}
    """)
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env)
    assert out == F.SelftestOutcome(ok=False, detail="signaled:31")


def test_selftest_fails_when_the_hook_raises(tmp_path):
    env = _hook_module(tmp_path, """
        def apply():
            raise RuntimeError("cannot isolate")
    """)
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env)
    assert out.ok is False and out.detail == "exit:3"


def test_selftest_fails_on_missing_hook():
    out = F.run_representative_selftest(isolation_hook="afx_no_such_module_xyz:apply")
    assert out.ok is False and out.detail == "exit:3"


def test_selftest_times_out_and_leaves_no_child(tmp_path):
    pid_file = tmp_path / "child.pid"
    env = _hook_module(tmp_path, f"""
        import os, time
        from agentic_fx.core import seccomp as S
        import selftest_isolation_hooks as H
        def apply():
            with open({str(pid_file)!r}, "w") as f:
                f.write(str(os.getpid()))
            out = H.isolate()
            time.sleep(60)
            return out
    """)
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env,
                                        timeout=3.0)
    pid = int(pid_file.read_text())
    still_there = Path(f"/proc/{pid}").exists()
    if still_there:
        os.kill(pid, 9)
    assert out == F.SelftestOutcome(ok=False, detail="timeout")
    assert not still_there


def test_selftest_runs_blas_with_four_threads(tmp_path):
    env = _hook_module(tmp_path, """
        import os
        import selftest_isolation_hooks as H
        def apply():
            if os.environ.get("OPENBLAS_NUM_THREADS") != "4":
                raise RuntimeError("BLAS is not multi-threaded")
            return H.isolate()
    """)
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env)
    assert out == F.SelftestOutcome(ok=True, detail="ok")


def test_workload_runs_threads_off_the_main_thread():
    got = F.representative_workload()
    assert got["threads_off_main"] is True


def test_selftest_detects_wrong_results(tmp_path):
    # 計算結果が oracle と違う環境の代わりに、workload の入力を壊す hook
    env = _hook_module(tmp_path, """
        from agentic_fx.core import runtime_fingerprint as F
        import selftest_isolation_hooks as H
        def apply():
            out = H.isolate()
            F._SMA_WINDOW = 19
            return out
    """)
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env)
    assert out.ok is False and out.detail.startswith("oracle_mismatch:")


def test_selftest_rejects_a_second_stacked_filter(tmp_path):
    # 先に緩い filter を積んでから allow/6 を掛けると Seccomp_filters は 2 になる
    env = _hook_module(tmp_path, """
        import dataclasses, os
        from agentic_fx.core import landlock, runtime_fingerprint as F, seccomp as S
        def apply():
            applied = landlock.apply_plugin_ruleset([])
            loose = dataclasses.replace(S.ALLOW6_TABLE, default_action=S.RET_ALLOW)
            S.install_program(S.build_program(loose, pid=os.getpid()))
            S.apply_allow6()
            return {**applied.attestation_fields(), "seccomp_profile": S.SECCOMP_PROFILE,
                    "sandbox_profile": F.SANDBOX_PROFILE_VERSION}
    """)
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env)
    assert out == F.SelftestOutcome(ok=False, detail="isolation_not_applied")


def test_selftest_timeout_returns_without_waiting_for_the_child(tmp_path):
    env = _hook_module(tmp_path, """
        import time
        def apply():
            time.sleep(60)
    """)
    t0 = time.monotonic()
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env,
                                        timeout=2.0)
    elapsed = time.monotonic() - t0
    assert out == F.SelftestOutcome(ok=False, detail="timeout")
    assert elapsed < 30, elapsed


# --- admission と process 内 cache ---------------------------------------------

class _CountingSelftest:
    def __init__(self, outcome=F.SelftestOutcome(ok=True, detail="ok"), *, exc=None,
                 delay=None):
        self.calls = 0
        self.outcome = outcome
        self.exc = exc
        self.delay = delay
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        if self.delay is not None:
            self.delay.wait(5)
        if self.exc is not None:
            raise self.exc
        return self.outcome


def _out_of_set():
    return dataclasses.replace(F.supported_runtimes()[0], landlock_abi=7)


def test_supported_fingerprint_is_admitted_without_selftest():
    st = _CountingSelftest()
    adm = F.RuntimeAdmission(selftest=st)
    d = adm.admit(F.supported_runtimes()[0])
    assert d == F.AdmissionDecision(admitted=True, supported=True, reason=None, warning=None)
    assert st.calls == 0


def test_out_of_set_runs_selftest_once_and_warns_on_success():
    st = _CountingSelftest()
    adm = F.RuntimeAdmission(selftest=st)
    first = adm.admit(_out_of_set())
    again = adm.admit(_out_of_set())
    assert first == again == F.AdmissionDecision(
        admitted=True, supported=False, reason=None, warning=F.OUT_OF_SET_WARNING)
    assert st.calls == 1


def test_out_of_set_failure_is_fixed_reason_and_cached():
    st = _CountingSelftest(F.SelftestOutcome(ok=False, detail="signaled:31"))
    adm = F.RuntimeAdmission(selftest=st)
    d1 = adm.admit(_out_of_set())
    d2 = adm.admit(_out_of_set())
    assert d1 == d2 == F.AdmissionDecision(
        admitted=False, supported=False, reason="runtime_fingerprint_selftest_failed",
        warning=None)
    assert st.calls == 1


def test_selftest_exception_is_a_failure():
    st = _CountingSelftest(exc=OSError("boom"))
    adm = F.RuntimeAdmission(selftest=st)
    d = adm.admit(_out_of_set())
    assert d.admitted is False and d.reason == "runtime_fingerprint_selftest_failed"
    assert adm.admit(_out_of_set()) == d
    assert st.calls == 1


def test_transient_selftest_failure_is_retried_after_the_interval():
    """一時要因 (timeout・spawn_failed) の失敗は恒久 cache しない。間隔内は再試行
    しないが、間隔を超えたら selftest をやり直す (環境が直れば許可へ戻れる)。"""
    clk = {"t": 0.0}
    st = _CountingSelftest(F.SelftestOutcome(ok=False, detail="timeout"))
    adm = F.RuntimeAdmission(selftest=st, retry_interval_sec=60.0, clock=lambda: clk["t"])
    d1 = adm.admit(_out_of_set())
    assert d1.admitted is False and d1.retryable is True
    assert d1.reason == "runtime_fingerprint_selftest_failed"
    clk["t"] = 59.0
    adm.admit(_out_of_set())
    assert st.calls == 1  # 間隔内は再試行しない
    clk["t"] = 61.0
    adm.admit(_out_of_set())
    assert st.calls == 2  # 間隔を超えたら再試行する


def test_permanent_selftest_failure_is_not_retried():
    """恒久要因 (計算不一致・隔離不成立など) は cache して再試行しない。"""
    clk = {"t": 0.0}
    st = _CountingSelftest(F.SelftestOutcome(ok=False, detail="workload_failed"))
    adm = F.RuntimeAdmission(selftest=st, retry_interval_sec=60.0, clock=lambda: clk["t"])
    d = adm.admit(_out_of_set())
    assert d.retryable is False
    clk["t"] = 10_000.0
    adm.admit(_out_of_set())
    assert st.calls == 1


def test_each_fingerprint_has_its_own_cache_entry():
    st = _CountingSelftest()
    adm = F.RuntimeAdmission(selftest=st)
    a = _out_of_set()
    b = dataclasses.replace(a, pandas=dataclasses.replace(a.pandas, record_sha256="1" * 64))
    adm.admit(a)
    adm.admit(b)
    adm.admit(a)
    assert st.calls == 2


def test_concurrent_first_callers_share_one_selftest():
    gate = threading.Event()
    st = _CountingSelftest(delay=gate)
    adm = F.RuntimeAdmission(selftest=st)
    results = []
    threads = [threading.Thread(target=lambda: results.append(adm.admit(_out_of_set())))
               for _ in range(8)]
    for t in threads:
        t.start()
    gate.set()
    for t in threads:
        t.join(10)
    assert len(results) == 8 and len(set(results)) == 1
    assert st.calls == 1


def test_admission_requires_a_selftest():
    with pytest.raises(TypeError):
        F.RuntimeAdmission()


def test_real_admission_of_this_host_out_of_set_path():
    # この host の実 fingerprint を ABI 7 として扱い、集合外の経路を実 harness で 1 回通す
    fp = dataclasses.replace(F.compute_fingerprint(landlock_abi=8), landlock_abi=7)
    adm = F.RuntimeAdmission(selftest=lambda: F.run_representative_selftest(
        isolation_hook=_FULL, env=_env()))
    d = adm.admit(fp)
    assert d == F.AdmissionDecision(admitted=True, supported=False, reason=None,
                                    warning=F.OUT_OF_SET_WARNING)


def test_module_does_not_import_numpy_or_pandas_at_import_time():
    code = ("import sys; import agentic_fx.core.runtime_fingerprint; "
            "print('numpy' in sys.modules, 'pandas' in sys.modules)")
    import subprocess
    out = subprocess.run([sys.executable, "-P", "-c", code], capture_output=True, text=True,
                         timeout=60, check=True).stdout.split()
    assert out == ["False", "False"]


# --- 子に渡す環境変数 ------------------------------------------------------------

def _env_reporting_hook(tmp_path: Path) -> tuple[dict, Path]:
    out_file = tmp_path / "child_environ.json"
    env = _hook_module(tmp_path, f"""
        import json, os
        import selftest_isolation_hooks as H
        def apply():
            with open({str(out_file)!r}, "w") as f:
                json.dump(dict(os.environ), f)
            return H.isolate()
    """)
    return env, out_file


def test_selftest_child_does_not_inherit_the_parent_secrets(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("AFX_TEST_SECRET_TOKEN", "secret-value-xyz")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-value-abc")
    monkeypatch.setenv("HOME", str(tmp_path))
    env, out_file = _env_reporting_hook(tmp_path)
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env)
    assert out == F.SelftestOutcome(ok=True, detail="ok")
    child_env = json.loads(out_file.read_text())
    assert "AFX_TEST_SECRET_TOKEN" not in child_env
    assert "ANTHROPIC_API_KEY" not in child_env
    assert "secret-value" not in json.dumps(child_env)
    assert set(child_env) <= {"PATH", "PYTHONPATH", "OPENBLAS_NUM_THREADS", "LC_CTYPE"}
    assert child_env["OPENBLAS_NUM_THREADS"] == "4"


def test_selftest_child_uses_the_env_given_explicitly(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("AFX_TEST_SECRET_TOKEN", "secret-value-xyz")
    env, out_file = _env_reporting_hook(tmp_path)
    env["AFX_TEST_EXPLICIT"] = "given"
    out = F.run_representative_selftest(isolation_hook="afx_selftest_hook:apply", env=env)
    assert out.ok
    child_env = json.loads(out_file.read_text())
    assert child_env["AFX_TEST_EXPLICIT"] == "given"
    assert "AFX_TEST_SECRET_TOKEN" not in child_env
