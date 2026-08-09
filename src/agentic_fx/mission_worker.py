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

`settings.runner.<profile>.backend` が "local" 以外 (= "claude") の場合は
`RuntimeError` で fail closed する (ClaudeRunner は本プランでは実装しない —
Global Constraints)。trade は `runner.trade.backend`、improve は
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
import signal
import sys
import sysconfig
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from agentic_fx.core import landlock
from agentic_fx.core.mission_protocol import (
    ProtocolError, SeqTracker, encode_frame, read_frame,
)

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

def _bootstrap_improve_profile() -> None:
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
    するが、improve profile は Landlock が唯一の FS 境界であるため必須。
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
    # venv/stdlib の外にある実行時依存。**allowlist は防御の質そのものなので
    # 最小に保つ**が、**「起動できる」ではなく「Mission を完走できる」を
    # 基準に測ること** (2026-08-09 実測):
    #
    #   /usr/lib             外すと `ImportError: libgcc_s.so.1: cannot open
    #                        shared object file` で improve worker が起動不能
    #   /usr/share/zoneinfo  タイムゾーンデータ。外すと起動不能
    #   /dev                 `/dev/urandom` (乱数生成) のため。**ディレクトリ
    #                        単位でしか許可できない** — `landlock.restrict_to`
    #                        は対象を `O_PATH | O_DIRECTORY` で open するので
    #                        単一ファイル (`/dev/urandom`) を渡すと
    #                        `NotADirectoryError` になる (実測)
    #   /etc                 **glibc の名前解決 (`/etc/nsswitch.conf`,
    #                        `/etc/hosts`) に必要。** 外すと
    #                        `socket.getaddrinfo("localhost", 8080)` が
    #                        `gaierror: Temporary failure in name resolution`
    #                        になり、既定の `llama_swap.base_url`
    #                        (`http://localhost:8080/v1`) へ到達できず、
    #                        **improve Mission が初回ターンで必ず failed に
    #                        なる** (レビュー 2 周目 `/code-review` が検出)
    #
    # **`/etc` を一度削除した経緯 (同じ誤りを繰り返さないために残す)**:
    # 指揮者の最初の最小化は「1 つずつ外して `test_real_improve_worker_
    # reaches_ready` が red になるか」で測ったが、**この test は `ready`
    # 送出までしか到達せず、その 1 行あとの `runner.run` (= 実際に LLM を
    # 叩く経路) を通らない**。名前解決はその先で初めて必要になるので、
    # 「外しても green」= 「不要」と読み違えた。いまは probe が
    # `getaddrinfo` まで見る (`test_improve_profile_cannot_reach_data_dir`)。
    #
    # **既知の制約**: `/etc/resolv.conf` は `/run/systemd/resolve/...` への
    # symlink なので、`/etc` を許可しても**外部ホスト名の DNS 解決はできない**
    # (実測)。`localhost`/IP は `/etc/hosts` で解決するので既定構成では問題に
    # ならない。`llama_swap.base_url` を外部ホスト名にする場合は allowlist の
    # 追加が要る。
    #
    # 外しても Mission 完走に影響が無かったため削除したもの: `/lib`・`/lib64`
    # (どちらも `/usr/lib`・`/usr/lib64` への symlink)、`/usr/lib64`。
    # 残した 4 つはいずれも `data/` の祖先ではないため、設計書 §4.6 の
    # 「`data/` の絶対パスアクセスを OS レベルで遮断する」意味論は保たれる
    # (`test_improve_profile_cannot_reach_data_dir` が毎回それを実測する)。
    for sys_path in [Path("/usr/lib"), Path("/usr/share/zoneinfo"),
                     Path("/dev"), Path("/etc")]:
        if sys_path.exists():
            read_only.append(sys_path)
    _assert_allowlist_excludes_data_dir(read_only + [workdir])
    try:
        landlock.restrict_to(read_only_paths=read_only, read_write_paths=[workdir])
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


def _assert_allowlist_excludes_data_dir(paths: list[Path]) -> None:
    """allowlist のどれ 1 つも `data/` の祖先 (または `data/` 自身) でない
    ことを確認し、違反したら **fail closed** する (プラン8 Task 18)。

    **なぜ必要か (段0 変異スイープで実測した 2 つの生存変異)**:

    - `WorkerRunner` の `Popen(..., cwd=workdir)` から `cwd=` を落とすと、子の
      cwd は親の cwd (= リポジトリ root) になり、`Path.cwd()` が
      **read-write** allowlist に入って `data/` が書込可能になる。
      フルスイート 1683 件は全 green のままだった
    - `code_root` を `parents[1]` (= `src/`) から `parents[2]` (= リポジトリ
      root) に広げると `data/` が読取可能になる。これも全 green のままだった

    どちらも「allowlist の計算を間違えた」という同じ形の事故なので、**計算
    結果そのものを不変条件として検査する**のがテストより確実な防御になる。
    テスト側 (`test_allowlist_never_covers_the_data_dir`) はこの検査自体が
    消されないことを pin する。

    **これは best-effort の第 2 層であり、第 1 層の代わりにはならない**
    (レビュー 2 周目 `/code-review` の指摘): 本番の data root は
    **サービスプロセスの cwd** (`entry.py` の `root = Path.cwd()`) であって
    このモジュールの配置ではない。`afx` は console script なのでリポジトリ
    外から起動する運用も正当で、そのとき `_guarded_data_dir()` は実在しない
    `<repo>/data` を指し、**この検査は素通りする**。非 editable install
    (wheel 配置) でも `parents[2]` は site-packages の親になる。
    したがって「子の cwd が専用 workdir であること」は**親側で保証する**のが
    本筋で (`WorkerRunner.run` の `Popen(cwd=...)` と
    `test_child_cwd_is_a_dedicated_dir_outside_the_repository`)、この検査は
    その配線が壊れたときに**運が良ければ捕まえる**最後の網に留まる。
    """
    data_dir = _guarded_data_dir()
    for p in paths:
        resolved = Path(p).resolve()
        # 祖先・一致に加えて**子孫も弾く** (`data/` 配下を workdir にする
        # 経路。レビュー 2 周目 `/code-review` の指摘)。
        if (resolved == data_dir or resolved in data_dir.parents
                or data_dir in resolved.parents):
            raise RuntimeError(
                f"improve worker allowlist would expose the history data "
                f"directory: {resolved} covers or lives under {data_dir} — "
                "refusing to start (fail closed, 設計書 §4.6)")


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
            _bootstrap_improve_profile()
            _set_resource_limits(
                as_mb=settings_dict["worker"]["child_as_mb"],
                nofile=settings_dict["worker"]["child_nofile"],
                fsize_mb=settings_dict["worker"]["child_fsize_mb"])
            from agentic_fx.config import Settings
            settings = Settings.model_validate(settings_dict)
            if settings.runner.improve.backend != "local":
                raise RuntimeError(
                    f"runner.improve.backend={settings.runner.improve.backend!r} "
                    "is not supported by mission_worker in this plan "
                    "(ClaudeRunner is Plan 9 scope) — fail closed")
            from agentic_fx.runners.base import Mission
            from agentic_fx.runners.local_runner import LocalRunner
            from agentic_fx.tools.registry import ToolRegistry

            registry = ToolRegistry()
            mission = Mission(**handshake["mission"])
            on_message = _make_on_message(protocol_out, out_seq)
            runner = LocalRunner(
                base_url=settings.llama_swap.base_url,
                model=settings.runner.improve.model, registry=registry,
                on_message=on_message)

            _send_frame(protocol_out, out_seq, {"type": "ready", "ok": True})
            ready_sent = True
            try:
                result = runner.run(mission)
                _send_frame(protocol_out, out_seq, {
                    "type": "result",
                    "status": result.status, "output": result.output})
            except Exception as exc:  # noqa: BLE001
                _send_frame(protocol_out, out_seq, {
                    "type": "result", "status": "failed", "output": None,
                    "error": f"{type(exc).__name__}: {exc}"})
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
        if settings.runner.trade.backend != "local":
            raise RuntimeError(
                f"runner.trade.backend={settings.runner.trade.backend!r} is "
                "not supported by mission_worker in this plan (ClaudeRunner "
                "is Plan 9 scope) — fail closed")

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
        from agentic_fx.runners.local_runner import LocalRunner

        mission = Mission(**handshake["mission"])

        on_message = _make_on_message(protocol_out, out_seq)

        runner = LocalRunner(
            base_url=settings.llama_swap.base_url,
            model=settings.runner.trade.model, registry=registry,
            on_message=on_message)

        _send_frame(protocol_out, out_seq, {"type": "ready", "ok": True})
        ready_sent = True

        try:
            result = runner.run(mission)
            _send_frame(protocol_out, out_seq, {
                "type": "result",
                "status": result.status, "output": result.output})
        except Exception as exc:  # noqa: BLE001 — 必ず result を送る
            _send_frame(protocol_out, out_seq, {
                "type": "result", "status": "failed", "output": None,
                "error": f"{type(exc).__name__}: {exc}"})
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
