"""Mission worker 子プロセス本体 (プラン8) — python -m agentic_fx.mission_worker
として起動される。`runners/worker_runner.py` (親, Task 10) が spawn する唯一の
想定呼び出し元。

**起動順序は厳守** (plugin/worker.py と同じ規律):
1. handshake (最初の 1 行) を読む → **type/seq を検証する** (I2 対応:
   親→子方向専用の `in_seq` トラッカーで `type == "handshake"` かつ
   `seq == 1` を確認。不一致は `ProtocolError` — 以降の `try` 節に含めて
   fail closed で `ready: false` を返す)
2. `_set_pdeathsig` (親死亡時に OS が SIGTERM を配送) → 設定**直後**に
   `os.getppid()` を `expected_parent_pid` と照合し、不一致 (= 設定前に
   親が死んで再親付けされた) なら即終了する (設計書 §4.8 codex I2-4)
3. resource limit (RLIMIT_AS/NOFILE/FSIZE/CORE=0) を設定 — **設定失敗は
   fail closed** (worker_profile="trade" は全項目)。CPU 制限は付けない
   (Mission の消費は LLM 待ちの壁時計であり親の preemption が受け持つ —
   設計書 §4.5)
4. `build_mission_registry` でツール配線を組み立てる (RAG は `_RagRpcProxy`
   を注入。**`out_seq` を先に構築し、`_RagRpcProxy` と `on_message` の
   両方に同一インスタンスを共有させる** — CR-3 対応。独立した
   `SeqTracker` を `_RagRpcProxy` に持たせると、親側 `WorkerRunner` の
   `in_seq` (子→親の全フレーム種別を単一トラッカーで検証) と衝突し、
   RAG 検索ツールの初回呼出しで必ず `ProtocolError` になっていた)
5. `ready` を送出
6. `LocalRunner.run(mission)` を実行 (`on_message` が `event` フレームを
   送出)
7. `result` を送出して終了

**対応 profile は `trade` と `improve` の 2 つ** (プラン8 Task 18 で improve を
追加。それ以外の値は `ready: ok=False` で fail closed する)。上記 1〜7 の流れは
`trade` のもので、`improve` は **DB にも plugin にも触れない別経路**を通る —
`_bootstrap_improve_profile()` で Landlock を適用してから、空の `ToolRegistry()`
で `LocalRunner` を組む (改善ループの実ツールセットはプラン 9)。

`settings.runner.<profile>.backend` は `runner_factory.build_runner()` が
選ぶ (local/claude/codex、trade は codex を `config.py` の
`_trade_backend_not_codex` が起動前に拒否済み — I-1 是正)。CLI backend
(claude/codex) のときは `_start_mcp_dispatcher` で `afx.sock` を `ready`
送出前に bind する。trade は `runner.trade.backend`、improve は
`runner.improve.backend` をそれぞれ見る。

**I4 対応 (実時計)**: 子は Mission 実行中の鮮度判定 (`get_signals` 等)
に `_build_clock()` (既定 `SystemClock()`) を使う。handshake 時刻で
`FixedClock` に固定すると、Mission 後半でも鮮度境界の基準時刻が進まず、
実時刻なら範囲外になったはずの古い signal が残り続ける fail-open になる
(fork レビュー I4)。テストは `mission_worker._build_clock` を
monkeypatch して `FixedClock` を注入できる (seam は残す)。
"""
from __future__ import annotations

import ctypes
import json
import os
import queue
import signal
import sys
import sysconfig
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NamedTuple

from agentic_fx.core import landlock
from agentic_fx.core.landlock import _assert_allowlist_excludes_data_dir
from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, encode_frame, read_frame,
)
# precheck 2026-08-22 pass2: RB3 (Step 7d) — factory.build_runner への
# monkeypatch を可能にするため module level import (test 側から
# `mw_mod.runner_factory` として属性差し替え)。
from agentic_fx.runners import factory as runner_factory
# A-4 検収是正 (2026-08-22, B1): McpShimDispatcher の module level import。
# `_start_mcp_dispatcher` から使う。ToolRegistry も同様に module level へ
# 上げる (`_build_improve_registry` の型注釈・既定実装で使うため — 従来
# `_run_improve_mission` 内の関数内 import だったものを引き上げた)。
from agentic_fx.tools.mcp_shim import McpShimDispatcher
from agentic_fx.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from agentic_fx.core.contracts import Clock

_PR_SET_PDEATHSIG = 1


def _build_clock() -> "Clock":
    """本番は実時計を使う (I4 対応)。テストはこの関数を monkeypatch して
    `FixedClock` を注入できる (seam)。"""
    from agentic_fx.core.contracts import SystemClock
    return SystemClock()


def _set_pdeathsig(sig: int) -> None:
    libc = ctypes.CDLL("libc.so.6", use_errno=True)
    if libc.prctl(_PR_SET_PDEATHSIG, sig, 0, 0, 0) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"prctl(PR_SET_PDEATHSIG) failed: "
                             f"{os.strerror(errno)}")


def _set_resource_limits(*, as_mb: int, nofile: int, fsize_mb: int) -> None:
    import resource

    as_bytes = int(as_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (as_bytes, as_bytes))
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(nofile), int(nofile)))
    fsize_bytes = int(fsize_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

def _bootstrap_improve_profile(
    *, backend: str, mission_id: str, staging_dir: str,
    source_snapshot_dir: str, claude_bin: str | None, codex_bin: str | None,
    opencode_bin: str | None = None,
) -> None:
    """improve worker profile の bootstrap (プラン8, 設計書 §4.6)。

    **⚠ CRITICAL: Landlock は不可逆。この関数を呼ぶプロセスの生涯全体が
    制限される。テストから呼ぶ場合は必ず `subprocess` を経由し、
    in-process (pytest プロセス内) で実行してはいけない。**

    **構造的到達不能の 2 層防御**: ①接続情報の非提供 (handshake に
    db_path/plugins_dir が含まれない — `main()` の improve 分岐がこれらを
    一切参照しない) ②Landlock による FS 自己制限 (コードツリー読取 +
    venv/stdlib 読取 (裁定書 F-8/IM-2/P8-04 — 実行に必要な依存解決のため) +
    専用 workdir 読書きのみ allowlist、`data/` は遮断)。

    呼び出し時点の `Path.cwd()` は WorkerRunner が `cwd=` に渡した専用空
    workdir (呼び出し元の責務 — このプロセス自身は検証しない)。

    **Landlock 利用不能な環境では improve worker は起動拒否 (fail
    closed)** — trade profile は Landlock を任意 (RO 接続が主防御) と
    するが, improve profile は Landlock が唯一の FS 境界であるため必須。
    """
    if not landlock.is_available():
        raise RuntimeError(
            "Landlock is not available on this kernel/architecture — "
            "improve worker profile refuses to start without it "
            "(fail closed, 設計書 §4.6)")

    code_root = Path(__file__).resolve().parents[1]
    workdir = Path.cwd()
    venv_root = Path(sys.prefix).resolve()
    stdlib_root = Path(sysconfig.get_paths()["stdlib"]).resolve()
    base_prefix = Path(sys.base_prefix).resolve()
    read_only = [code_root, venv_root, stdlib_root]
    if base_prefix != venv_root:
        read_only.append(base_prefix)
    for sys_path in [Path("/usr/lib"), Path("/usr/share/zoneinfo"), Path("/etc")]:
        if sys_path.exists():
            read_only.append(sys_path)
    if Path("/run/systemd/resolve").exists():
        read_only.append(Path("/run/systemd/resolve"))
    if backend in ("claude", "opencode"):
        # opencode (bun/JSC) は /proc/self/maps 読取に失敗すると SIGABRT
        # (検収実測 2026-08-30: mmap 予約は全て成功した状態で maps/cgroup
        # EACCES 直後に自己 abort)。claude と同じリスク受容 (R10 参照)。
        read_only.append(Path("/proc"))

    # staging_dir の相互照合 (§2.2): 末尾成分が mission_id と一致するか。
    # dirfd で開き所有者/mode/種別を再検証してから rw に加える。
    staging_path = Path(staging_dir).resolve()
    if staging_path.name != mission_id:
        raise RuntimeError(
            f"staging_dir {staging_path} does not match mission_id "
            f"{mission_id!r} — refusing to start (fail closed, 設計書 §2.2)")
    fd = os.open(str(staging_path), os.O_DIRECTORY | os.O_PATH)
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or (st.st_mode & 0o777) != 0o700:
            raise RuntimeError(
                f"staging_dir {staging_path} failed re-verification "
                f"(uid/mode) — refusing to start (fail closed)")
    finally:
        os.close(fd)

    # source_snapshot_dir verification (Blocking 10)
    source_snapshot_path = Path(source_snapshot_dir).resolve()
    if (not source_snapshot_path.is_dir()
            or workdir.resolve() not in source_snapshot_path.parents
            and source_snapshot_path != workdir.resolve()):
        raise RuntimeError(
            f"source_snapshot_dir {source_snapshot_path} is not a real "
            f"directory under workdir {workdir} — refusing to start "
            "(fail closed, 設計書 §2.2)")

    closure = _exec_closure_for(
        backend, claude_bin=Path(claude_bin) if claude_bin else None,
        codex_bin=Path(codex_bin) if codex_bin else None,
        # opencode の既定 bin は "~/.opencode/bin/opencode" (チルダ入り)。
        # expanduser しないと後段の .resolve() が workdir 相対に化ける
        # (検収実測 2026-08-30: /tmp/afx-mission-*/~/.opencode/... で ENOENT)
        opencode_bin=(Path(opencode_bin).expanduser()
                      if opencode_bin else None), venv_root=venv_root)
    execute_dirs = list(closure.dirs)
    # `_exec_closure_for` の docstring どおり base_prefix は呼び出し側
    # (このモジュール) が合成する — venv の `sys.executable` が指す実体
    # (base_prefix 配下の実 python) を exec できないと venv 内 python の
    # 自己 exec が PermissionError になる (5-C 申し送り、Task 6
    # gate_pytest_worker.py の同型パターンと同じ)。
    if base_prefix != venv_root:
        execute_dirs.append(base_prefix)

    # PT_INTERP 解決は **Landlock 適用前・この境界で** 行う (5-C 改訂
    # 2026-08-22, 裁定 A)。非 ELF (`#!/usr/bin/env node` 型のシェバン
    # ラッパ等) は RuntimeError で fail closed — staging の uid/mode
    # 検証と同じ扱い。
    execute_files = landlock.interpreter_files_for(closure.targets)

    _assert_allowlist_excludes_data_dir(
        read_only + [workdir, staging_path] + execute_dirs + execute_files,
        guarded_data_dir=_guarded_data_dir())
    try:
        landlock.restrict_to(
            read_only_paths=read_only, read_write_paths=[workdir, staging_path,
                                                          Path("/dev")],
            execute_paths=execute_dirs, execute_file_paths=execute_files)
    except landlock.LandlockUnavailable as e:
        raise RuntimeError(
            f"Landlock restriction failed (syscall error): {e} "
            "(improve worker profile refuses to continue, fail closed)") from e


def _guarded_data_dir() -> Path:
    """遮断対象である `data/` の位置を、**handshake からではなくこのモジュール
    自身の配置から**導く (`<repo>/src/agentic_fx/mission_worker.py` →
    `<repo>/data`)。

    improve worker は接続情報を渡されない (防御層①) ので `root` を知らない。
    それでも「**許可してはいけない場所**」は知ることができる — 知るのが禁止
    なのは到達手段であって、禁止領域の座標ではない。

    **`code_root` から導いてはいけない** (レビュー 1 周目の KAT が「`parents[1]`
    と `parents[2]` で 2 段違うのは紛らわしい」と指摘した点への回答): この
    ガードが塞いでいる事故の 1 つは **`code_root` の計算を誤って広げること**
    そのもの (段0 M14)。`data/` の位置を `code_root` から導くと、`code_root`
    がずれたときにガードの基準も一緒にずれて**検査が空振りする**。両者は
    意図的に独立の式で、`__file__` という同じ 1 点からそれぞれ導く。
    """
    return Path(__file__).resolve().parents[2] / "data"


class ExecClosure(NamedTuple):
    """`_exec_closure_for` の返り値 (プラン10 Task 5 5-C 改訂, 2026-08-22,
    裁定 A)。`dirs` はディレクトリ単位で EXECUTE を与える対象、`targets` は
    exec される実ファイル (PT_INTERP 解決の入力) — 「ディレクトリ単位の
    EXECUTE」と「ファイル単位の EXECUTE」というマスク差 (= セキュリティ上の
    差異) を型に残す。PT_INTERP 解決 (I/O) は `_bootstrap_improve_profile`
    側で行う (`_exec_closure_for` 自身は I/O をしない純関数のまま)。"""
    dirs: list[Path]
    targets: list[Path]


def _exec_closure_for(backend: str, *, claude_bin: Path | None,
                      codex_bin: Path | None, venv_root: Path,
                      opencode_bin: Path | None = None) -> ExecClosure:
    """§2.2 の表。**local backend には EXECUTE をディレクトリ単位で `/usr/lib`
    に与えない** — 与えると `/usr/lib` 配下に実体を持つ実行ファイル
    (uutils coreutils 等) がすべて exec 可能になり、shell 遮断が無効化される
    (実測、5-C 改訂 2026-08-22)。動的ローダは**実ファイル 1 個**を
    `execute_file_paths` で付与する (呼び出し側が `targets` から
    `landlock.interpreter_files_for` で解決する)。共有ライブラリの
    mmap(PROT_EXEC) に EXECUTE は不要 (READ_FILE で足りる — `/usr/lib` は
    read_only 側に残す)。

    **claude/codex は従来どおり `/usr/lib` を含む** (裁定 4) — これらは
    §2.1-5 により `/usr/bin` + shell を意図的に許可しており、`/usr/lib` を
    落とすと `git submodule` (`/usr/lib/git-core/`) 等が壊れる一方、
    得られる安全性はほぼ無い。

    I/O はしない (PT_INTERP 解決は呼び出し側)。
    """
    dirs: list[Path] = [venv_root]                     # base_prefix は呼び出し側で合成
    targets: list[Path] = [Path(sys.executable).resolve()]
    if backend in ("claude", "codex", "opencode"):
        dirs.append(Path("/usr/bin"))
        if Path("/bin").is_dir() and not Path("/bin").is_symlink():
            dirs.append(Path("/bin"))
        for p in (Path("/usr/lib"), Path("/usr/lib64")):
            if p.exists():
                dirs.append(p)
    if backend == "claude" and claude_bin is not None:
        real = claude_bin.resolve()
        dirs.append(real.parent)
        targets.append(real)
    if backend == "codex" and codex_bin is not None:
        real = codex_bin.resolve()
        dirs.append(real.parent)
        targets.append(real)
    if backend == "opencode" and opencode_bin is not None:
        real = opencode_bin.resolve()
        dirs.append(real.parent)
        targets.append(real)
    return ExecClosure(dirs=dirs, targets=targets)


class _RagRpcProxy:
    """`news_tools.build(rag)`/`reflection_tools.build(conn, rag, pairs)`
    が要求する duck-type 契約 (`search_news`/`search_reflections`) のみを
    実装する (chromadb PersistentClient は多プロセス同時アクセス非対応 —
    設計書 §4.4)。`tool_rpc` は常に同時 1 件以下、同期ブロッキング待ち
    (設計書 §4.3)。

    `out_seq` (CR-3 対応) — 子→親方向 (`ready`/`event`/`tool_rpc`/
    `result`) の全フレームは `main()` が保持する単一の `SeqTracker` を
    共有する。`_RagRpcProxy` が自前の独立した `SeqTracker` を持つと、
    親側 `WorkerRunner.in_seq` (子→親の全フレーム種別を単一トラッカーで
    検証 — 設計書 §4.3 codex M2-1) の期待値と衝突し、RAG 検索ツールを
    使う Mission が初回呼出しで必ず `ProtocolError` になっていた
    (レビュー CR-3)。

    `in_seq` (I2 対応) — 親→子方向 (`tool_rpc_result`) 専用の受信検証
    トラッカー。`main()` が `handshake` の検証にも同じインスタンスを
    使う (親→子方向は 1 起点で共通)。**この共有の帰結として、本番では
    親が送る最初の `tool_rpc_result` の seq は 2 になる** (seq=1 は
    handshake が消費済み) — Task 10 の親側実装はこれに合わせること。"""

    def __init__(self, write_fn: Callable[[dict], None],
                read_fn: Callable[[], dict | None],
                out_seq: SeqTracker, in_seq: SeqTracker) -> None:
        self._write = write_fn
        self._read = read_fn
        self._out_seq = out_seq
        self._in_seq = in_seq
        self._rpc_counter = 0

    def _call(self, name: str, args: dict) -> Any:
        self._rpc_counter += 1
        rpc_id = str(self._rpc_counter)
        # レビュー 1 周目 (codex I-2): seq は**送出が成功してから**進める。
        # 先に消費すると、write/flush が失敗したときにその seq が wire に
        # 出ないまま欠番になり、次に成功したフレームを親の `SeqTracker` が
        # reject する。
        seq = self._out_seq._expected  # noqa: SLF001 — 送出側は採番に使う
        self._write({"type": "tool_rpc", "seq": seq,
                     "rpc_id": rpc_id, "name": name, "args": args})
        self._out_seq._expected += 1  # noqa: SLF001
        response = self._read()
        if response is None:
            raise RuntimeError("parent closed the pipe while awaiting tool_rpc_result")
        # I2 対応: type/seq を検証してから中身を信用する。
        if response.get("type") != "tool_rpc_result":
            raise ProtocolError(
                f"expected tool_rpc_result, got {response.get('type')!r}")
        self._in_seq.check(response.get("seq"))
        if response.get("rpc_id") != rpc_id:
            raise RuntimeError(
                f"tool_rpc_result rpc_id mismatch: expected {rpc_id}, "
                f"got {response.get('rpc_id')!r}")
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "rag rpc failed")))
        return response.get("result")

    def search_news(self, query: str, n: int = 5) -> list[dict]:
        return self._call("search_news", {"query": query, "n": n})

    def search_reflections(self, query: str, n: int = 5) -> list[dict]:
        return self._call("search_reflections", {"query": query, "n": n})


def _make_on_message(protocol_out: Any, out_seq: SeqTracker) -> Callable[[dict], None]:
    """`LocalRunner(on_message=...)` に渡すコールバックを組み立てる
    (I6 対応 — 独立関数に切り出してあるのは単体テストで `os._exit` を
    monkeypatch し、write 失敗時の fail-closed 経路を `main()` 全体を
    実行せずに検証するため)。"""
    def on_message(msg: dict) -> None:
        try:
            _send_frame(protocol_out, out_seq, {"type": "event", "message": msg})
        except Exception:  # noqa: BLE001 — I6 対応 (fail closed)
            # LocalRunner._sink (Task 6) は on_message の例外を握って
            # run() を継続する契約 — しかし event フレームを送れないまま
            # 実行を続けると、親は transcript の一部を永久に受け取れない
            # (レビュー I6)。fail-soft に「継続」させず、このプロセスを
            # 即座に終了する (親は EOF/予期しない終了として Mission を
            # 失敗させる — 既に壊れた状態で run() を続けても無意味)。
            #
            # レビュー 2 周目 (codex) 以降、transport 失敗は
            # `_write_frame_or_die` がプロセスごと落とすため、この節へ
            # 実際に到達するのは **serialize 失敗** (transcript の message
            # が JSON にならない) のとき。その場合 seq は未消費なので
            # 欠番にはならないが、送れなかった事実は変わらないので
            # 同じく fail closed にする。
            os._exit(1)
    return on_message


def _protect_protocol_stdout() -> Any:
    """JSON 行プロトコル専用の書き込み先を確保し、fd 1 (stdout) を fd 2
    (stderr) へ付け替えて返す (`plugin/worker.py:_protect_protocol_stdout`
    と同じパターン — import 済みライブラリの意図しない `print`/警告出力が
    プロトコルの JSON 1 行ストリームに混ざるのを防ぐ)。**何よりも先に**
    (`read_frame` より前) 呼ぶ — import 時の副作用による stdout 汚染も
    ここで防がれる対象に含める。
    """
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return protocol_out


def _make_rpc_client(protocol_out: Any, out_seq: SeqTracker,
                     in_seq: SeqTracker) -> Callable[[str, dict], Any]:
    """`run_backtest`/`analyze_corr` の親呼び出し RPC client。フレーム
    送受信の規律 (rpc_id 採番・seq 検証・エラー伝播) は `_RagRpcProxy._call`
    (:307-330) と一字一句同じにする。呼び出し元
    (`_build_improve_registry` が組む registry の `rpc_handlers`) は
    `McpShimDispatcher._call_lock` (`tools/mcp_shim.py:38`) 経由で常に
    同時 1 件以下しか呼ばれない — `_RagRpcProxy` が単一の
    `sys.stdin.buffer` 読取を安全に共有できているのと同じ前提。"""
    rpc_counter = {"n": 0}

    def call(name: str, args: dict) -> Any:
        rpc_counter["n"] += 1
        rpc_id = str(rpc_counter["n"])
        seq = out_seq._expected  # noqa: SLF001 — 送出側は採番に使う
        _send_frame(protocol_out, out_seq,
                   {"type": "tool_rpc", "rpc_id": rpc_id, "name": name,
                    "args": args})
        response = read_frame(sys.stdin.buffer)
        if response is None:
            raise RuntimeError(
                "parent closed the pipe while awaiting tool_rpc_result")
        if response.get("type") != "tool_rpc_result":
            raise ProtocolError(
                f"expected tool_rpc_result, got {response.get('type')!r}")
        in_seq.check(response.get("seq"))
        if response.get("rpc_id") != rpc_id:
            raise RuntimeError(
                f"tool_rpc_result rpc_id mismatch: expected {rpc_id}, "
                f"got {response.get('rpc_id')!r}")
        if not response.get("ok"):
            raise RuntimeError(str(response.get("error", "rpc failed")))
        return response.get("result")
    return call


def _build_improve_registry(*, settings: Any, workdir: Path, staging_dir: Path,
                            source_snapshot_dir: Path,
                            rpc_client: Callable[[str, dict], Any]
                            ) -> ToolRegistry:
    """improve profile 用 `ToolRegistry` の構築 (A-4 検収是正、裁定 R-D2)。

    A14 裁定 (2026-08-28、束D検収 verified-local-round1.md §7):
    `rpc_client` を必須引数化した。旧実装は `rpc_client=None` の既定値で
    空 `ToolRegistry()` を返す fail-open 経路を持っていた (Task 4/A-4 段階
    の互換 seam) が、対して `tools/mission_registry.py::build_mission_
    registry` は `rpc_handlers is None` で `ValueError` を送出する
    (fail closed) — 非対称だった。本番の唯一の呼び出し
    (`_run_improve_mission`) は常に `_make_rpc_client(...)` の戻り値を
    渡すため実害は無かったが、既定値を消して呼び出し元に明示させる。"""
    from agentic_fx.loops.improve_rpc_ledger import ImproveRpcLedger
    from agentic_fx.tools.mission_registry import build_mission_registry

    child_ledger = ImproveRpcLedger(rpc_timeout_sec_by_kind={
        "run_backtest": settings.improve.backtest_rpc_timeout_sec,
        "analyze_corr": settings.improve.backtest_rpc_timeout_sec})
    return build_mission_registry(
        "improve", None, settings, None, None, activity=None,
        staging_dir=staging_dir, source_snapshot_dir=source_snapshot_dir,
        ledger=child_ledger,
        rpc_handlers={"run_backtest": lambda a: rpc_client("run_backtest", a),
                     "analyze_corr": lambda a: rpc_client("analyze_corr", a)})


def mcp_socket_path(workdir: Path) -> Path:
    """improve worker の MCP dispatcher socket パス (A-4 検収是正 r2、B2-r2)。

    `_start_mcp_dispatcher` が bind するパスと、CLI 側
    (`CliRunner.run`) が `_build_argv` へ渡す `mcp_socket` は、**この関数
    1 本**から導出する。前回是正 (r2 以前) は両者が独立した
    `workdir / "afx.sock"` リテラルとして別々の場所に書かれており、
    `cli_runner.py` 側のパス式のみを変異させても検出できなかった
    (検収 B2-r2: 全スイートで Survived)。式を 1 本に統合することで、
    どちら側の呼び出しを変異させても不一致が実プロセス経由で検出できる。
    """
    return workdir / "afx.sock"


def _start_mcp_dispatcher(*, workdir: Path, registry: ToolRegistry) -> McpShimDispatcher:
    """improve worker 側の Unix socket dispatcher を起動する
    (A-4 検収是正 Step 7a/7b、設計書 §1.6、r2 是正 B1-r2/RW6)。

    `McpShimDispatcher.bind()` を**呼び出しスレッドで同期実行**する —
    bind の成否をそのまま例外として受け取れるため、以前の実装
    (`serve_forever` を daemon thread へ投げてから `sock_path.exists()` を
    3 秒ポーリングする代理観測) が持っていた 2 つの欠陥が同時に消える:
    (a) 代理観測は「そのパスに何か在るか」しか見ておらず、bind 前から
    別エントリ (他プロセスの残骸やディレクトリ) が同名で存在すると
    誤って成功と判定していた (masking probe、検収 B1-r2 (a))。
    (b) daemon thread の非同期 bind がテスト間で cwd を共有する
    in-process 呼び出し (`main()` を直接駆動する improve profile テスト)
    と競合し、全スイートを flaky にしていた (検収 B1-r2 (b))。
    同期 bind によりポーリング・3 秒 deadline は不要になる。

    bind (+ registry 構築) が完了してからこの関数が返ることで、呼び出し元
    (`_run_improve_mission` → `main()`) が `ready` フレームを送出する時点
    では既に dispatcher が accept 可能な状態にある (RW6: 子は
    「bind + registry 構築 → ready 送出 → go 待ち」の順)。
    """
    sock_path = mcp_socket_path(workdir)
    dispatcher = McpShimDispatcher(
        sock_path=sock_path, registry=registry, allowed=registry.names())
    try:
        dispatcher.bind()
    except OSError as e:
        # bind の失敗 (Landlock 拒否、ENOENT、既存エントリがディレクトリ
        # 等) をそのまま呼び出し元へ fail closed で伝える。黙って見逃すと
        # CLI へ存在しない socket path を渡し続け、B1 (bind されない
        # socket = ツール 0 個) を再発させる。
        raise RuntimeError(
            f"MCP dispatcher failed to bind {sock_path} — refusing to "
            "start (fail closed: CLI would run with 0 tools, 検収 B1)") from e
    thread = threading.Thread(target=dispatcher.serve_forever, daemon=True)
    thread.start()
    return dispatcher


def _wait_for_go(in_seq: "SeqTracker", timeout_sec: float) -> bool:
    """`ready` 送出後、`go` フレーム (裁定 RW1) を受信するまで待つ。
    `worker_startup_timeout_sec` 内に届かなければ False を返す — 呼び出し
    元 (`main()`) はこの場合 Mission もツールも実行せず、`result` フレーム
    も送らずに終了する (`go` 前は副作用ゼロ、設計書 §1)。

    `sys.stdin.buffer` の `readline()` はブロッキングであり、かつ
    `BufferedReader` の内部先読みが `select()` の fd 監視をすり抜けうる
    (`go` が届いた時点で既に内部バッファへ読み込まれている可能性がある)
    ため、`select`/`signal.alarm` ではなく**別スレッド + `queue.Queue`**
    でタイムアウトを実装する (`worker_runner.py` の `_wait_with_stop` と
    同じ発想 — daemon thread がタイムアウト後もブロックし続けても、
    プロセス終了時に道連れで消える、FC-1 と同型の許容)。"""
    result_queue: "queue.Queue[dict | None]" = queue.Queue(maxsize=1)

    def _reader() -> None:
        try:
            frame = read_frame(sys.stdin.buffer)
        except ProtocolError:
            frame = None
        result_queue.put(frame)

    threading.Thread(target=_reader, daemon=True,
                     name="afx-mission-go-waiter").start()
    try:
        frame = result_queue.get(timeout=timeout_sec)
    except queue.Empty:
        return False  # worker_startup_timeout_sec 超過 — 副作用ゼロで終了
    if frame is None or frame.get("type") != "go":
        return False  # EOF (親が落ちた) / 不正フレーム — fail closed
    in_seq.check(frame.get("seq"))
    return True


def _run_improve_mission(
    *, settings: Any, workdir: Path, staging_dir: str,
    source_snapshot_dir: str, protocol_out: Any = None, out_seq: Any = None,
    in_seq: Any = None
) -> Any:
    """improve profile での runner 構築・Mission 実行 (Step 7d + A-4 検収
    是正 Step 7a/7b)。

    `_build_improve_registry` で registry を組み立て、`_start_mcp_dispatcher`
    で同じ `workdir` に `afx.sock` を bind してから、factory.build_runner
    経由で backend (local/claude/codex) を選択し runner を構築する。
    unit test は protocol_out/out_seq をデフォルト None で呼び出し、
    monkeypatch で factory.build_runner (と必要なら `_build_improve_registry`)
    を spy/差し替えする。main() は実際の値を渡し、返された runner を run する。

    段 0 申し送り 2: `_start_mcp_dispatcher` の戻り値 (dispatcher) を
    `runner` に紐づけて呼び出し元へ返す — `_run_improve_mission` 自身は
    Mission を実行しない (呼び出し元の `main()` が `runner.run(mission)`
    する) ため、dispatcher の close は呼び出し元の責務。旧実装は戻り値を
    破棄しており、参照が `serve_forever` の daemon thread (bound method)
    だけになるため、GC のタイミングに socket の生死が暗黙に依存していた。
    `runner._afx_mcp_dispatcher` に保持することで、既存の `runner = ...`
    呼び出し側 (テスト含む) の呼び出し形は変えずに済む。

    :param settings: Settings インスタンス
    :param workdir: Mission 実行用 workdir (Landlock rw 許可範囲)
    :param protocol_out: stdout プロトコル出力 (main で生成、フレーム送信用)
    :param out_seq: SeqTracker インスタンス (protocol_out 送信時に採番用)
    :return: AgentRunner インスタンス (`_afx_mcp_dispatcher` 属性に
        `McpShimDispatcher` を保持する)
    """
    rpc_client = _make_rpc_client(protocol_out, out_seq, in_seq)
    registry = _build_improve_registry(
        settings=settings, workdir=workdir, staging_dir=Path(staging_dir),
        source_snapshot_dir=Path(source_snapshot_dir), rpc_client=rpc_client)
    dispatcher = _start_mcp_dispatcher(workdir=workdir, registry=registry)
    on_message = _make_on_message(protocol_out, out_seq)
    runner = runner_factory.build_runner(
        "improve", settings, registry, workdir=workdir,
        on_message=on_message,
        # precheck 2026-08-22 pass2: RB3 (裁定 R1 の 2 段配線のうち後段) — CLI
        # (claude/codex) が spawn した子の pgid を、既存の out_seq フレーム
        # 送出経路 (_send_frame) に相乗りさせて親 (WorkerRunner) へ通知する。
        # local backend では build_runner がこの引数を無視するため無害。
        cli_started_sink=lambda pgid: _send_frame(
            protocol_out, out_seq, {"type": "cli_started", "pgid": pgid}))
    runner._afx_mcp_dispatcher = dispatcher
    return runner


# M1 是正 (プラン10 束D round1、verified-codex-round1.md、2026-08-28):
# `_wait_for_go` の完全重複定義 (`656a5dc` での再転写混入、`5723ad8` の
# 初出が正) をここで削除した。使用点 (`main()`) は前方の定義 (裁定名
# 「RW1」を明記した docstring の方) を束縛する。挙動・テストとも不変
# (本文は完全同一、差分は docstring 1 行のみだった)。


def main() -> None:
    protocol_out = _protect_protocol_stdout()

    # I2 対応: 親→子方向 (handshake/tool_rpc_result) 専用の受信検証
    # トラッカー。handshake は常に seq=1 (親の唯一の起動時送出)。
    in_seq = SeqTracker()
    # CR-3 対応: 子→親方向の ready/event/tool_rpc/result は 1 起点の単一
    # カウンタを共有する (設計書 §4.3 codex M2-1、親側 WorkerRunner.in_seq
    # がそう検証する)。レビュー 1 周目 (codex I-2) で `main()` の先頭へ
    # 移動した — 外側 `except` からも採番できる必要があるため。
    out_seq = SeqTracker()
    ready_sent = False
    try:
        # レビュー 1 周目 (codex I-1): handshake の読み取りを `try` の**内側**
        # へ移した。旧実装は `try` の外で `read_frame` を呼んでいたため、
        # 不正 JSON の handshake が `ProtocolError` を素通しさせて traceback
        # で異常終了し、**`ready: ok=False` を一切返さなかった** (親からは
        # 起動 timeout と区別がつかない)。EOF (None) は従来どおり正常終了。
        handshake = read_frame(sys.stdin.buffer)
        if handshake is None:
            return
        if handshake.get("type") != "handshake":
            raise ProtocolError(
                f"expected handshake, got {handshake.get('type')!r}")
        in_seq.check(handshake.get("seq"))

        expected_parent_pid = handshake["expected_parent_pid"]
        _set_pdeathsig(signal.SIGTERM)
        if os.getppid() != expected_parent_pid:
            # 設計書 §4.8 codex I2-4: prctl 設定前に親が死んで再親付け
            # されたレース。ready を送らずに即終了する (親の起動 timeout
            # がこれを検出する)。
            return
        settings_dict = handshake["settings"]
        worker_profile = handshake["worker_profile"]
        if worker_profile == "improve":
            improve_backend = settings_dict["runner"]["improve"]["backend"]
            if improve_backend == "claude":
                claude_bin = settings_dict["runner"]["claude"]["bin"]
                codex_bin = None
                # 検収是正 (2026-08-30): opencode_bin の代入漏れ — この分岐
                # だけ未代入だと backend=claude が UnboundLocalError で死ぬ
                opencode_bin = None
            elif improve_backend == "codex":
                claude_bin = None
                codex_bin = settings_dict["runner"]["codex"]["bin"]
                opencode_bin = None
            elif improve_backend == "opencode":
                claude_bin = None
                codex_bin = None
                opencode_bin = settings_dict["runner"]["opencode"]["bin"]
            else:
                claude_bin = None
                codex_bin = None
                opencode_bin = None
            _bootstrap_improve_profile(
                backend=improve_backend,
                mission_id=handshake["mission_id"],
                staging_dir=handshake["staging_dir"],
                source_snapshot_dir=handshake["source_snapshot_dir"],
                claude_bin=claude_bin, codex_bin=codex_bin,
                opencode_bin=opencode_bin)
            # 検収実測 (2026-08-30): opencode (bun/JSC) は `run` 時に巨大な
            # 仮想アドレス予約を行う (strace 実測: 8GB 一括 (gigacage)、
            # 16GB 制限下では 128GB 一括の arena 予約が ENOMEM → SIGABRT)。
            # 予約は仮想アドレスのみで実メモリ消費ではないが、この規模では
            # RLIMIT_AS は封じ込めとして機能しない — opencode backend の
            # ときだけ 256GB へ引き上げる (実質的な暴走抑止は NOFILE/FSIZE/
            # timeout が担う)。他 backend の既定 4096MB は緩めない。
            as_mb = settings_dict["worker"]["child_as_mb"]
            if improve_backend == "opencode":
                as_mb = max(as_mb, 262144)
            _set_resource_limits(
                as_mb=as_mb,
                nofile=settings_dict["worker"]["child_nofile"],
                fsize_mb=settings_dict["worker"]["child_fsize_mb"])
            from agentic_fx.config import Settings
            settings = Settings.model_validate(settings_dict)
            from agentic_fx.runners.base import Mission

            workdir = Path.cwd()
            mission = Mission(**handshake["mission"])
            runner = _run_improve_mission(
                settings=settings, workdir=workdir,
                staging_dir=handshake["staging_dir"],
                source_snapshot_dir=handshake["source_snapshot_dir"],
                protocol_out=protocol_out, out_seq=out_seq, in_seq=in_seq)

            _send_frame(protocol_out, out_seq, {
                "type": "ready", "ok": True,
                # レビュー2周目 Important 3: 子が実際に受領・相互照合を通した3値を
                # ready event へ乗せて返す (診断用途、trade profile では付与しない)。
                "run_context": {
                    "mission_id": handshake["mission_id"],
                    "staging_dir": handshake["staging_dir"],
                    "source_snapshot_dir": handshake["source_snapshot_dir"],
                },
            })
            ready_sent = True
            # precheck 2026-08-23 wave3: RW1/RW6 — go 前は副作用ゼロ。
            # runner.run(mission) (= LLM 起動) は go を受けてから呼ぶ。
            if not _wait_for_go(
                    in_seq, settings.worker.worker_startup_timeout_sec):
                return  # timeout/EOF/不正フレーム — result も送らず終了
            try:
                try:
                    result = runner.run(mission)
                    _send_frame(protocol_out, out_seq, {
                        "type": "result",
                        "status": result.status, "output": result.output,
                        "reason": result.reason})
                except Exception as exc:  # noqa: BLE001
                    _send_frame(protocol_out, out_seq, {
                        "type": "result", "status": "failed", "output": None,
                        "error": f"{type(exc).__name__}: {exc}"})
            finally:
                # 段 0 申し送り 2: `_run_improve_mission` が保持した
                # dispatcher を Mission 終了時 (成功/失敗いずれの経路でも)
                # に close する。`ready` 前後の送出順序はここでは一切
                # 触れない — bind→ready→go の順序 (RW6) は
                # `_start_mcp_dispatcher`/上の `_send_frame` 呼び出し順の
                # ままで、close は `runner.run()` 完了後の後始末のみ。
                dispatcher = getattr(runner, "_afx_mcp_dispatcher", None)
                if dispatcher is not None:
                    dispatcher.close()
            return
        if worker_profile != "trade":
            raise RuntimeError(
                f"unsupported worker_profile in this plan: {worker_profile!r}")
        _set_resource_limits(
            as_mb=settings_dict["worker"]["child_as_mb"],
            nofile=settings_dict["worker"]["child_nofile"],
            fsize_mb=settings_dict["worker"]["child_fsize_mb"])

        from agentic_fx.config import Settings
        settings = Settings.model_validate(settings_dict)

        # R2/B6 (設計書 §2.2): trade worker は handshake の credentials を
        # os.environ へ setenv する — datafeed コード (price_provider.py/
        # sources.py) が TWELVEDATA_API_KEY/MT5_BRIDGE_API_KEY を env
        # 直接参照するため。improve 分岐では行わない (裁定書 F-9 の遮断維持)。
        for _cred_key, _cred_value in (handshake.get("credentials") or {}).items():
            os.environ[_cred_key] = _cred_value

        from agentic_fx.store.db import connect_readonly
        from agentic_fx.tools import plugin_loader
        from agentic_fx.tools.mission_registry import build_mission_registry
        from agentic_fx.activity import ActivityLog

        conn = connect_readonly(Path(handshake["db_path"]))
        # I4 対応: 本番は実時計を使う (handshake["now"] で FixedClock に
        # 固定すると、Mission 後半でも鮮度判定の基準時刻が進まず
        # get_signals 等の鮮度検証が fail-open になる — レビュー I4)。
        clock = _build_clock()
        # 子は自身の scratch workdir に activity.log を持つ (§4.6 のとおり
        # econ.upcoming() は self.activity に触れないため実際には書かれ
        # ないが、EconCalendar のコンストラクタ契約を満たすためだけに
        # 必要 — mission_registry.py の docstring 参照)。
        activity = ActivityLog(Path.cwd() / "activity.log")
        plugins_dir = (Path(handshake["plugins_dir"])
                       if handshake.get("plugins_dir") else None)
        approved = (plugin_loader.approved_plugins(conn, plugins_dir)
                   if plugins_dir is not None else [])

        # CR-3 対応: `main()` 冒頭で構築した out_seq を、_RagRpcProxy と
        # on_message (event フレーム送出) の両方に**同一インスタンス**で
        # 共有させる。
        registry = build_mission_registry(
            "trade", conn, settings, clock,
            _RagRpcProxy(
                lambda frame: _write_frame_or_die(protocol_out, frame),
                lambda: read_frame(sys.stdin.buffer),
                out_seq, in_seq),
            activity=activity, indicator_plugins=approved, readonly=True)

        from agentic_fx.runners.base import Mission

        mission = Mission(**handshake["mission"])

        on_message = _make_on_message(protocol_out, out_seq)

        # I-1 是正: trade も improve と同じ factory.build_runner 経由で
        # backend (local/claude) を選ぶ (config.py の
        # `_trade_backend_not_codex` が codex を既に拒否済み)。CLI
        # backend (claude) のときは improve と同形で `afx.sock` を
        # `ready` 送出前に bind する — CLI に渡す `--mcp-config` の
        # socket が bind 済みであることを保証するため (段 0 検収の
        # 「ツール 0 個で走る」欠落の是正)。
        trade_dispatcher = None
        if settings.runner.trade.backend != "local":
            trade_dispatcher = _start_mcp_dispatcher(
                workdir=Path.cwd(), registry=registry)
        runner = runner_factory.build_runner(
            "trade", settings, registry, workdir=Path.cwd(),
            on_message=on_message,
            cli_started_sink=lambda pgid: _send_frame(
                protocol_out, out_seq, {"type": "cli_started", "pgid": pgid}))
        if trade_dispatcher is not None:
            runner._afx_mcp_dispatcher = trade_dispatcher

        _send_frame(protocol_out, out_seq, {"type": "ready", "ok": True})
        ready_sent = True

        try:
            try:
                result = runner.run(mission)
                _send_frame(protocol_out, out_seq, {
                    "type": "result",
                    "status": result.status, "output": result.output,
                    "reason": result.reason})
            except Exception as exc:  # noqa: BLE001 — 必ず result を送る
                _send_frame(protocol_out, out_seq, {
                    "type": "result", "status": "failed", "output": None,
                    "error": f"{type(exc).__name__}: {exc}"})
        finally:
            dispatcher = getattr(runner, "_afx_mcp_dispatcher", None)
            if dispatcher is not None:
                dispatcher.close()
    except Exception as exc:  # noqa: BLE001 — ready 送出前の失敗も報告する
        try:
            # レビュー 1 周目 (codex I-2): 旧実装は無条件に
            # `{"type": "ready", "seq": 1, ...}` を送っていた。`ready`
            # (seq=1) を送出**済み**でこの節に到達する経路が実在する
            # (内側 except 自身の送出が失敗した場合) ため、親の
            # `SeqTracker` から見て seq=1 の**重複**になっていた。
            # 送出済みなら `result: failed` として報告する。
            if ready_sent:
                _send_frame(protocol_out, out_seq, {
                    "type": "result", "status": "failed", "output": None,
                    "error": f"{type(exc).__name__}: {exc}"})
            else:
                _send_frame(protocol_out, out_seq, {
                    "type": "ready", "ok": False,
                    "error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001 — パイプが壊れていれば諦める
            pass


def _write_frame_or_die(protocol_out: Any, frame: dict) -> None:
    """1 フレームを protocol stream へ書く。**transport 失敗なら即プロセス終了**。

    レビュー 2 周目 (codex): `write`/`flush` の例外は「フレームが wire に
    1 バイトも出ていない」ことを**保証しない**。部分書込みの後に失敗して
    いれば、同じ seq で別フレームを送り直すと `{"type":...` の途中に別の
    JSON が連結されて**行が壊れる**。flush がデータを相手へ渡した後に失敗
    したのなら、親は既に最初のフレームを受理しており、再送は**重複**に
    なる。どちらが起きたかは呼び出し側から判定できない。

    したがって transport 失敗は回復不能として扱い、**再送せずに子を即座に
    終了する** (fail closed)。親は EOF / 異常終了として Mission を失敗
    させる — これは `_make_on_message` (I6) が既に採っている方針と同じ。

    serialize 失敗 (`encode_frame` の `TypeError` 等) は**ここへ来る前に**
    送出され、ストリームには触れない。呼び出し側はその場合だけ同じ seq で
    別フレームを送り直してよい。
    """
    line = encode_frame(frame)  # serialize 失敗はここ (wire 未接触 → 再送可)
    try:
        protocol_out.write(line)
        protocol_out.flush()
    except Exception:  # noqa: BLE001 — 配信有無が不明なので回復を試みない
        os._exit(1)


def _send_frame(protocol_out: Any, out_seq: SeqTracker, frame: dict) -> None:
    """子→親のフレームを 1 件送出する (`seq` はここで採番して付す)。

    **カウンタは送出が成功してから進める** (レビュー 1 周目 codex I-2)。
    旧実装 (`_next_seq`) は serialize/write/flush より**前**に消費していた
    ため、送出が失敗するとその seq が wire に出ないまま欠番になり、次に
    成功したフレームを親の `SeqTracker` が `ProtocolError` として reject
    した。実測: `runner.run()` の戻り値が壊れていて result フレームの
    構築中に落ちると、wire 上は `ready(1)` の次が `result(3)` になった。

    「送出が成功していないなら seq を進めない」が安全なのは **serialize
    失敗のときだけ** — transport 失敗は `_write_frame_or_die` がプロセス
    ごと落とすため、そもそも呼び出し側に戻らない (レビュー 2 周目 codex)。

    `SeqTracker` を採番器として流用する意図は Task 7 本文のとおり
    (「次に来る/送り出すべき値」の意味が受信検証と送信採番で一致する)。
    """
    payload = dict(frame)
    payload["seq"] = out_seq._expected  # noqa: SLF001 — 送出側は採番に使う
    _write_frame_or_die(protocol_out, payload)
    out_seq._expected += 1  # noqa: SLF001


if __name__ == "__main__":
    main()
