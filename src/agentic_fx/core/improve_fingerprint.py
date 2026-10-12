"""改善 mission の CLI 隔離の fingerprint: 隔離の構成と実行 artifact を正規形にして hash する。

要素ごとに SHA-256 を持ち、要素名と hash の並びの SHA-256 を aggregate とする。要素は次のとおり
(どれか 1 つが変われば aggregate が変わり、admission は判定をやり直す)。

| 要素 | 取り方 |
|---|---|
| profile | seccomp profile 版 (`improve-cli/1`) |
| seccomp_policy | canonical table の policy hash |
| bpf | 生成 BPF の byte hash と filter flags |
| nested_landlock | nested ruleset の正規形の digest |
| outer_landlock | outer 規則を組み立てる code (bootstrap から参照を辿った関数・定数と `landlock.py`) と役割の表 |
| egress | egress 定数の digest |
| source_* | launcher (artifact の copy を含む)・protocol・supervisor・proxy・spool・cgroup・canary (自己試験の verify backend を含む)・isolation の source file の bytes |
| kernel | Landlock ABI、seccomp の flag 可否、PIDFD_THREAD、Yama、INET_DIAG |
| userspace | kernel release、machine、Python 実行系、libc |
| artifact | 内容の SHA-256 と (dev, ino, size, mtime) |
| host_artifact | codex の隣の code-mode host の artifact、他 backend は `none` |
| package_manifest | codex package manifest の bytes hash、他 backend は `none` |
| launch_kind | codex は `path_fd`、他 backend は `native_elf` |
| argv_env_policy | CLI の argv / env を組む code と、隔離時に足す・除く env の方針 (値は入れない) |

isolation の群は worker 側で隔離を組み立てる code (起動順・stdin の読み手・cleanup の
系列の呼び出し・admission の判定)。`improve_isolation.py`・`improve_admission.py`・`mission_worker.py`
は file 全体を hash する (mission worker の隔離の配線は `main()` の中に直に書かれていて、関数単位で
切り出すと `main()` 全体と同じになる。admission は判定の部品が module の大半を占める)。そのため
trade の経路や有効化の定数 (`ISOLATION_ENABLED`) を変えても aggregate は変わる。memfd の封印は
launcher が使う `cli_artifact_copy.py` にあり、launcher の群で hash する。この module は群に
入れない (入れると対応済み集合を足す変更そのものが aggregate を変えてしまう)。

source は `.py` の bytes を読む (`.pyc` ではない)。対応済み fingerprint の集合は空で、足す経路を
持たない (集合に入れるのは、最終の profile で全自己試験を済ませた後の code 変更だけ)。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import platform
import stat
import sys
import sysconfig
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

#: 対応済み fingerprint (aggregate) の集合。実行時に足す経路は作らない。登録値は afx 起動時の
#: 技術ログ 1 行 (`improve isolation: enabled; backend=… admitted=yes fingerprint=…`) を写す。
#: この module 自体は source hash の対象外なので、ここを足しても aggregate は変わらない。
#: hash 対象 (source / artifact / userspace / kernel) のどれかが変わると集合外になり、admission は
#: canary と自己試験をやり直す (登録は次の集合外 → 合格の後に更新する)。
#: 2026-10-11 登録 (最終 E2E: 3 backend の実 improve mission が隔離下で完走):
SUPPORTED_FINGERPRINTS: frozenset[str] = frozenset({
    # codex (vendor native ELF 0.162.1、path_fd。npm の再インストールで inode / mtime が変わると
    # 集合外になる。2026-10-12 の再インストール後の本番起動行から再登録)
    "2a07fd8ca694368a110dba6ed85c51288db432aa578addf037d59f322c31abce",
    # claude (2.1.296、native_elf)
    "28fb9fa7d8626fd012374cb8920d7dd120cc774ce6ccca4d25d7ade61ceec23a",
    # opencode (native_elf)
    "ab20d92fbb2a18a66b772199283faa7652507a79bde78cf3519fdaf173971ab2",
})

#: `src/agentic_fx` (source hash の基準)
SOURCE_ROOT = Path(__file__).resolve().parents[1]

LAUNCH_KIND_NATIVE_ELF = "native_elf"
LAUNCH_KIND_PATH_FD = "path_fd"

BACKENDS = ("claude", "codex", "opencode")

#: source hash の群。proxy は egress package の全 `.py` (後から足した module も含める)
SOURCE_GROUPS: Mapping[str, tuple[str, ...]] = {
    "launcher": ("core/cli_launcher.py", "core/cli_artifact_copy.py"),
    "protocol": ("core/cli_launch_protocol.py", "core/cli_launch.py"),
    "supervisor": ("core/connect_supervisor.py",),
    "proxy": ("egress/*.py",),
    "spool": ("tools/mcp_spool.py", "tools/mcp_spool_client.py", "tools/mcp_shim.py"),
    "cgroup": ("core/mission_cgroup.py", "core/mission_cleanup.py", "core/cgroup_host.py"),
    "canary": ("core/improve_canary.py", "loops/verify_backend.py"),
    "isolation": ("core/improve_isolation.py", "core/improve_admission.py", "mission_worker.py"),
}

# --- env の方針 (値は持たない。隔離時に CLI env へ足す key と除く key) --------------------

#: claude / codex に足す proxy の key (値は `http://127.0.0.1:<proxy port>`)
PROXY_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
#: proxy を経ない宛先の key (loopback だけ。supervisor は 8080 以外の loopback を拒否する)
NO_PROXY_ENV_KEYS = ("NO_PROXY", "no_proxy")
NO_PROXY_VALUE = "localhost,127.0.0.1"
#: npm の wrapper が足す managed 環境変数。native ELF は未設定で完走するので渡さない
EXCLUDED_ENV_PREFIXES = ("CODEX_MANAGED_",)


def env_policy(backend: str) -> dict:
    """隔離時の CLI env の方針 (fingerprint の材料)。"""
    from agentic_fx.egress import policy as egress_policy

    if backend not in BACKENDS:
        raise ValueError(f"unknown backend {backend!r}")
    proxied = backend != "opencode"
    return {
        "backend": backend,
        "proxy_keys": list(PROXY_ENV_KEYS) if proxied else [],
        "no_proxy_keys": list(NO_PROXY_ENV_KEYS) if proxied else [],
        "no_proxy_value": NO_PROXY_VALUE if proxied else None,
        "proxy_value": "http://127.0.0.1:<proxy port>" if proxied else None,
        "stripped_keys": list(PROXY_ENV_KEYS + NO_PROXY_ENV_KEYS) if not proxied else [],
        "excluded_prefixes": list(EXCLUDED_ENV_PREFIXES),
        "llama_swap_url": egress_policy.LLAMA_SWAP_URL if backend == "opencode" else None,
    }


def isolated_cli_env(backend: str, env: Mapping[str, str], proxy_port: int | None) -> dict:
    """runner が組んだ env に隔離の方針を当てる。

    claude / codex は proxy の URL を足す。opencode は proxy の key を消す (8080 へ直接)。
    managed 環境変数はどの backend にも渡さない。"""
    out = {k: v for k, v in env.items()
           if not any(k.startswith(p) for p in EXCLUDED_ENV_PREFIXES)}
    if backend == "opencode":
        for k in PROXY_ENV_KEYS + NO_PROXY_ENV_KEYS:
            out.pop(k, None)
        return out
    if proxy_port is None:
        raise ValueError(f"{backend} needs the proxy port")
    url = f"http://127.0.0.1:{int(proxy_port)}"
    for k in PROXY_ENV_KEYS:
        out[k] = url
    for k in NO_PROXY_ENV_KEYS:
        out[k] = NO_PROXY_VALUE
    return out


# --- artifact -----------------------------------------------------------------------

class ArtifactChanged(OSError):
    """hash の途中で artifact の大きさか mtime が変わった。"""


@dataclass(frozen=True)
class ArtifactIdentity:
    """worker が開いた fd の内容の SHA-256 と metadata の正規化組。"""

    sha256: str
    dev: int
    ino: int
    size: int
    mtime_ns: int

    def as_dict(self) -> dict:
        return {"sha256": self.sha256, "dev": self.dev, "ino": self.ino, "size": self.size,
                "mtime_ns": self.mtime_ns}

    @classmethod
    def from_dict(cls, value: object) -> "ArtifactIdentity":
        if not isinstance(value, dict) or set(value) != {"sha256", "dev", "ino", "size",
                                                          "mtime_ns"}:
            raise ValueError("artifact identity must have exactly sha256/dev/ino/size/mtime_ns")
        sha = value["sha256"]
        if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef"
                                                             for c in sha):
            raise ValueError("artifact sha256 must be 64 lowercase hex digits")
        ints = [value[k] for k in ("dev", "ino", "size", "mtime_ns")]
        if any(not isinstance(i, int) or isinstance(i, bool) or i < 0 for i in ints):
            raise ValueError("artifact metadata must be non-negative integers")
        return cls(sha, *ints)


_HASH_CHUNK = 1 << 20


def hash_artifact_fd(fd: int) -> ArtifactIdentity:
    """開いた fd を先頭から pread で読んで SHA-256 を取る (file offset を動かさない)。

    path は開き直さない。読む前後の (size, mtime) が違えば `ArtifactChanged`。"""
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise ArtifactChanged(0, "artifact is not a regular file")
    h = hashlib.sha256()
    off = 0
    while True:
        chunk = os.pread(fd, _HASH_CHUNK, off)
        if not chunk:
            break
        h.update(chunk)
        off += len(chunk)
    after = os.fstat(fd)
    if (off != before.st_size or (after.st_size, after.st_mtime_ns)
            != (before.st_size, before.st_mtime_ns)):
        raise ArtifactChanged(0, "artifact changed while hashing")
    return ArtifactIdentity(h.hexdigest(), after.st_dev, after.st_ino, after.st_size,
                            after.st_mtime_ns)


# --- 要素 -----------------------------------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canon(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _group_files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for pat in patterns:
        if "*" in pat:
            files += sorted(root.glob(pat))
        else:
            files.append(root / pat)
    return files


def source_digest(group: str, root: Path | None = None) -> str:
    """群の source file の bytes を `相対 path \\0 sha256` の並びにして hash する。"""
    base = SOURCE_ROOT if root is None else root
    lines = []
    for path in _group_files(base, SOURCE_GROUPS[group]):
        rel = path.relative_to(base).as_posix()
        lines.append(f"{rel}\0{_sha(path.read_bytes())}\n")
    return _sha("".join(lines).encode())


def _function_sources(path: Path, names: tuple[str, ...]) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    found = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            found[node.name] = _sha((ast.get_source_segment(text, node) or "").encode())
    missing = set(names) - set(found)
    if missing:
        raise ValueError(f"{path.name} に {sorted(missing)} が無い")
    return found


#: outer Landlock の規則を組む入口。ここから参照を辿り、規則を決める source を全部集める
OUTER_LANDLOCK_ENTRY = ("mission_worker.py", "_bootstrap_improve_profile")
#: 参照先が丸ごと規則の部品である module (file の bytes 全体を hash する)
OUTER_LANDLOCK_WHOLE_FILES = ("core/landlock.py",)
_PACKAGE = "agentic_fx"


def _module_rel(module: str) -> str | None:
    """`agentic_fx.x.y` → `x/y.py` (package の外は None)。"""
    if module != _PACKAGE and not module.startswith(_PACKAGE + "."):
        return None
    parts = module.split(".")[1:]
    return "/".join(parts) + ".py" if parts else None


class _ModuleIndex:
    """1 module の top-level の定義 (関数・class・代入) と import の束縛。"""

    def __init__(self, base: Path, rel: str) -> None:
        self.rel = rel
        self.text = (base / rel).read_text(encoding="utf-8")
        self.defs: dict[str, ast.AST] = {}
        self.modules: dict[str, str] = {}             # 束縛名 → module の rel
        self.names: dict[str, tuple[str, str]] = {}   # 束縛名 → (module の rel, 名前)
        for node in ast.parse(self.text).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.defs[node.name] = node
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.defs[t.id] = node
            elif isinstance(node, ast.Import):
                for a in node.names:
                    rel_mod = _module_rel(a.name)
                    if rel_mod is not None and a.asname:
                        self.modules[a.asname] = rel_mod
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for a in node.names:
                    bound = a.asname or a.name
                    sub = _module_rel(f"{node.module}.{a.name}")
                    if sub is not None and (base / sub).is_file():
                        self.modules[bound] = sub
                    else:
                        owner = _module_rel(node.module)
                        if owner is not None:
                            self.names[bound] = (owner, a.name)

    def segment(self, name: str) -> str:
        return ast.get_source_segment(self.text, self.defs[name]) or ""


def outer_landlock_sources(root: Path | None = None) -> dict[str, str]:
    """outer Landlock の規則を決める source の hash (`file` か `file:名前` → SHA-256)。

    入口 (`_bootstrap_improve_profile`) の本体が参照する名前を辿り、同じ module の top-level の
    関数・class・定数と、package 内の別 module の属性・import した名前を集める (stdlib は
    userspace の要素が持つ)。`OUTER_LANDLOCK_WHOLE_FILES` の module は file 全体を hash する。
    bootstrap が新しい部品を参照し始めれば、ここに自動で入る。"""
    base = SOURCE_ROOT if root is None else root
    indexes: dict[str, _ModuleIndex] = {}

    def index(rel: str) -> _ModuleIndex:
        if rel not in indexes:
            indexes[rel] = _ModuleIndex(base, rel)
        return indexes[rel]

    out: dict[str, str] = {}
    todo = [OUTER_LANDLOCK_ENTRY]
    while todo:
        rel, name = todo.pop()
        if rel in OUTER_LANDLOCK_WHOLE_FILES:
            out.setdefault(rel, _sha((base / rel).read_bytes()))
            continue
        key = f"{rel}:{name}"
        if key in out:
            continue
        mod = index(rel)
        if name not in mod.defs:
            raise ValueError(f"{rel} に {name} が無い")
        out[key] = _sha(mod.segment(name).encode())
        for node in ast.walk(mod.defs[name]):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                    and node.value.id in mod.modules:
                todo.append((mod.modules[node.value.id], node.attr))
            elif isinstance(node, ast.Name):
                if node.id in mod.defs and node.id != name:
                    todo.append((rel, node.id))
                elif node.id in mod.names:
                    todo.append(mod.names[node.id])
                elif node.id in mod.modules and mod.modules[node.id] in OUTER_LANDLOCK_WHOLE_FILES:
                    todo.append((mod.modules[node.id], ""))
    return dict(sorted(out.items()))


def outer_landlock_form(backend: str, root: Path | None = None) -> dict:
    """outer Landlock の規則の形。mission ごとの path は役割名に置き換え、host の固定 path は
    在るものだけを並べる。規則を組む code (`outer_landlock_sources`) の hash を含めるので、
    規則の作り方が変われば digest も変わる。"""
    fixed_ro = [p for p in ("/usr/lib", "/usr/share/zoneinfo", "/etc", "/run/systemd/resolve")
                if os.path.exists(p)]
    exec_dirs = ["<venv>", "<base_prefix>"]
    exec_dirs += [p for p in ("/usr/bin", "/usr/lib", "/usr/lib64") if os.path.exists(p)]
    if os.path.isdir("/bin") and not os.path.islink("/bin"):
        exec_dirs.append("/bin")
    exec_dirs.append("<artifact_dir>")
    return {
        "backend": backend,
        "read_only": ["<code_root>", "<venv>", "<stdlib>", "<base_prefix>", *fixed_ro]
        + (["/proc"] if backend in ("claude", "opencode") else []),
        "read_write": ["<workdir>", "<staging>", "/dev", "<transcripts>"],
        "execute_dirs": exec_dirs,
        "execute_files": ["<pt_interp of python and artifact>"],
        "sources": outer_landlock_sources(root),
    }


def argv_env_policy_form(backend: str, root: Path | None = None) -> dict:
    """CLI の argv / env を組む code と隔離時の env の方針 (秘密の値は入れない)。"""
    base = SOURCE_ROOT if root is None else root
    return {
        "runner": _sha((base / "runners" / f"{backend}_runner.py").read_bytes()),
        "cli_runner": _sha((base / "runners" / "cli_runner.py").read_bytes()),
        "env_policy": env_policy(backend),
    }


def userspace_identity() -> dict:
    base = getattr(sys, "_base_executable", None) or sys.executable
    uname = os.uname()
    return {
        "kernel_release": uname.release,
        "machine": uname.machine,
        "python_version": platform.python_version(),
        "python_build": list(platform.python_build()),
        "python_executable": os.path.realpath(base),
        "libc": list(platform.libc_ver()),
        "stdlib": sysconfig.get_paths()["stdlib"],
    }


@dataclass(frozen=True)
class KernelCapabilities:
    """admission の probe の結果 (fingerprint の材料にも使う)。"""

    machine: str
    landlock_abi: int
    seccomp_missing: tuple[str, ...]
    supervisor_missing: tuple[str, ...]
    yama: str
    inet_diag: str  # "ok" か不可の理由

    def as_dict(self) -> dict:
        return {"machine": self.machine, "landlock_abi": self.landlock_abi,
                "seccomp_missing": list(self.seccomp_missing),
                "supervisor_missing": list(self.supervisor_missing),
                "yama": self.yama, "inet_diag": self.inet_diag}


def _read_yama() -> str:
    from agentic_fx.core import connect_supervisor as CS

    try:
        with open(CS.YAMA_PTRACE_SCOPE, encoding="ascii") as f:
            return f.read().strip()[:16]
    except FileNotFoundError:
        return "absent"
    except (OSError, UnicodeDecodeError):
        return "unreadable"


def probe_kernel() -> KernelCapabilities:
    """実 kernel に問う (install はしない)。"""
    from agentic_fx.core import cli_launcher, connect_supervisor, landlock_nested
    from agentic_fx.egress import inet_diag
    from agentic_fx.egress.proxy import ProxyStartFailed

    try:
        abi = landlock_nested.probe_abi()
    except OSError:
        abi = -1
    try:
        inet_diag.probe_inet_diag()
        diag = "ok"
    except ProxyStartFailed as e:
        diag = f"unavailable: {e}"[:200]
    return KernelCapabilities(
        machine=os.uname().machine, landlock_abi=abi,
        seccomp_missing=tuple(cli_launcher.probe_seccomp_capabilities()),
        supervisor_missing=tuple(connect_supervisor.probe_supervisor_capabilities()),
        yama=_read_yama(), inet_diag=diag)


@dataclass(frozen=True)
class ImproveFingerprint:
    components: tuple[tuple[str, str], ...]
    aggregate: str

    def component(self, name: str) -> str:
        return dict(self.components)[name]


COMPONENTS = ("profile", "seccomp_policy", "bpf", "nested_landlock", "outer_landlock", "egress",
              *(f"source_{g}" for g in SOURCE_GROUPS), "kernel", "userspace", "artifact",
              "host_artifact", "package_manifest", "launch_kind", "argv_env_policy")


@dataclass(frozen=True)
class CodexPackage:
    """codex の host (検査した fd の identity) と package manifest の bytes hash。"""
    host: ArtifactIdentity
    manifest_sha: str


def read_codex_package(artifact_path: str) -> CodexPackage:
    """path から host と manifest を読む (検査済みの値を持たない側が自分で計算するときだけ使う)。"""
    binary = Path(artifact_path)
    host = binary.parent / "codex-code-mode-host"
    manifest = binary.parent.parent / "codex-package.json"
    fd = os.open(host, os.O_RDONLY | os.O_CLOEXEC)
    try:
        ident = hash_artifact_fd(fd)
    finally:
        os.close(fd)
    return CodexPackage(ident, _sha(manifest.read_bytes()))


def component_values(backend: str, artifact: ArtifactIdentity, kernel: KernelCapabilities,
                     *, root: Path | None = None,
                     userspace: Mapping | None = None, artifact_path: str | None = None,
                     codex_package: CodexPackage | None = None) -> dict[str, object]:
    """各要素の正規形 (hash する前の値)。"""
    from agentic_fx.core import landlock_nested, seccomp_cli_bpf, seccomp_cli_policy
    from agentic_fx.egress import policy as egress_policy

    compiled = seccomp_cli_bpf.compile_policy()
    values: dict[str, object] = {
        "profile": seccomp_cli_policy.PROFILE_ID,
        "seccomp_policy": compiled.policy_hash,
        "bpf": {"bpf_hash": compiled.bpf_hash, "filter_flags": compiled.filter_flags},
        "nested_landlock": landlock_nested.ruleset_digest(landlock_nested.NESTED_RULESET),
        "outer_landlock": outer_landlock_form(backend, root),
        "egress": egress_policy.egress_constants_digest(),
    }
    for g in SOURCE_GROUPS:
        values[f"source_{g}"] = source_digest(g, root)
    values["kernel"] = kernel.as_dict()
    values["userspace"] = dict(userspace) if userspace is not None else userspace_identity()
    values["artifact"] = artifact.as_dict()
    values["host_artifact"] = "none"
    values["package_manifest"] = "none"
    if backend == "codex":
        if codex_package is None and artifact_path is not None:
            codex_package = read_codex_package(artifact_path)
        if codex_package is not None:
            values["host_artifact"] = codex_package.host.as_dict()
            values["package_manifest"] = codex_package.manifest_sha
    values["launch_kind"] = LAUNCH_KIND_PATH_FD if backend == "codex" else LAUNCH_KIND_NATIVE_ELF
    values["argv_env_policy"] = argv_env_policy_form(backend, root)
    return values


def _aggregate(comps: tuple[tuple[str, str], ...]) -> ImproveFingerprint:
    return ImproveFingerprint(comps, _sha("".join(f"{n}\t{h}\n" for n, h in comps).encode()))


def fingerprint_from_values(values: Mapping[str, object]) -> ImproveFingerprint:
    if list(values) != list(COMPONENTS):
        raise ValueError("fingerprint の要素が揃っていない")
    return _aggregate(tuple((name, _sha(_canon(values[name]))) for name in COMPONENTS))


def with_artifact(fp: ImproveFingerprint, artifact: ArtifactIdentity, *,
                  clear_codex_package: bool = False) -> ImproveFingerprint:
    """`fp` の artifact の要素だけを `artifact` に替えた fingerprint (他の要素は同じ値のまま)。

    canary は CLI 役に Python の ELF を使うので、worker に渡す fingerprint はこれで組む
    (worker は handshake の artifact で fingerprint を計算し直して照合する)。"""
    art = _sha(_canon(artifact.as_dict()))
    none = _sha(_canon("none"))
    return _aggregate(tuple((n, art if n == "artifact" else
                             none if clear_codex_package and n in
                             ("host_artifact", "package_manifest") else h)
                            for n, h in fp.components))


def compute_fingerprint(backend: str, artifact: ArtifactIdentity, kernel: KernelCapabilities,
                        *, root: Path | None = None,
                        userspace: Mapping | None = None,
                        artifact_path: str | None = None,
                        codex_package: CodexPackage | None = None) -> ImproveFingerprint:
    return fingerprint_from_values(
        component_values(backend, artifact, kernel, root=root, userspace=userspace,
                         artifact_path=artifact_path, codex_package=codex_package))


def is_supported(fp: ImproveFingerprint,
                 supported: Callable[[], frozenset[str]] | None = None) -> bool:
    return fp.aggregate in (SUPPORTED_FINGERPRINTS if supported is None else supported())


__all__ = [
    "BACKENDS", "COMPONENTS", "EXCLUDED_ENV_PREFIXES", "LAUNCH_KIND_NATIVE_ELF", "LAUNCH_KIND_PATH_FD",
    "NO_PROXY_ENV_KEYS", "NO_PROXY_VALUE", "PROXY_ENV_KEYS", "SOURCE_GROUPS", "SOURCE_ROOT",
    "SUPPORTED_FINGERPRINTS", "ArtifactChanged", "ArtifactIdentity", "ImproveFingerprint",
    "KernelCapabilities", "argv_env_policy_form", "component_values", "compute_fingerprint",
    "env_policy", "fingerprint_from_values", "hash_artifact_fd", "is_supported",
    "isolated_cli_env", "outer_landlock_form", "outer_landlock_sources", "probe_kernel",
    "source_digest", "userspace_identity", "with_artifact",
]
