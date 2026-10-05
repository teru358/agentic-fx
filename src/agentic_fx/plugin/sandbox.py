"""plugin コードをサンドボックス実行する (プラン 7 Task 2、設計書 §6)。

**脅威モデル (必読)**: `check_source` は AST 上の**名前出現**に基づく静的
検査であり、動的呼び出し (`getattr(obj, "e" + "val")` のような文字列組み
立て、オブジェクト内省による sandbox escape 等) を防ぐものではない。
`worker.py` の resource limit・import 遮断も同様に「善意だが不注意な
plugin コードの事故 (無限ループ・OOM・意図しない I/O)」を防ぐことが目的
であり、悪意ある攻撃者からの完全な隔離を保証しない。**最終防衛線は人間
承認** (§6 — plugin は承認されるまで discover/実行されず、承認は
content_hash 一致を人間がレビューした版に限定する)。

**同居実行の残余リスク ([indicator-consumption-wiring] §3)**: strategy と、
その依存 indicator は**同一 worker プロセス**で実行される (IPC を deps 倍に
しないため — 非ベクトル化 1 本で 60 秒 CPU 予算が破れた実測がある)。承認前の
strategy 候補と承認済 indicator が同居するため、以下の 3 面を追加で塞いでいる:
(i) module 到達 — `import` 遮断 + 一意名 import (`indicator_<alias>`) で
`sys.modules` 衝突も防ぐ (ii) df / params の mutation — call ごとに deep copy
(iii) pandas/numpy の**プロセス全体のグローバル状態** — `check_source` の
deny 名 (`set_option`/`reset_option`/`set_eng_float_format`/`seterr`/
`seterrcall`/`setbufsize`/`set_printoptions`) と「外部属性への代入・削除の
一律拒否」、加えて worker 側で `pd.get_option("mode.chained_assignment")` と
`np.geterr()` の call 前後不変を assert する。いずれも「善意だが不注意な
plugin の事故」を防ぐ多層防御であり、悪意ある攻撃者からの完全な隔離を
保証しない — **最終防衛線は人間承認**。

**実行時ハッシュ再検証 (プラン 7 Task 3 レビュー fix round 1 F1)**:
`PluginSession.__enter__` は worker 起動前に `loader.content_hash` で
plugin フォルダを再計算し `meta.content_hash` と照合する (TOCTOU 封鎖 —
起動時の承認チェックと実行時の import の間で plugin.py が差し替えられて
いないことを保証する)。詳細は `PluginSession.__enter__` の docstring。

**セッション型 IPC の設計 (opus R2 I1)**: バックテストは同一 plugin を
数千回評価するため、1 回の評価ごとに 1 プロセスを起動していては性能が
成立しない。`PluginSession` は worker サブプロセストを 1 個だけ起動し、
stdin/stdout の JSON 1 行ずつで `call()` を繰り返せる。`run_plugin` は
「セッション 1 回だけ」の薄いラッパで、producer 等の単発評価用。

**ワイヤ形式 (worker.py と対 — 変更する場合は両方のドキュメントを同期
すること。これは sandbox.py/worker.py だけが知っていればよい内部契約
であり、他モジュールから import して使うものではない)**:

起動は二段 (詳細と各行の形は worker.py の docstring):

1. 親は全 session 共通の admission (host の要求水準 → runtime fingerprint →
   集合外なら代表自己試験) を通し、`Popen` の直前に現在 thread の継承 seccomp
   filter を検査してから `python -B -m agentic_fx.plugin.worker` を起こす。
2. handshake (`attest_nonce`・`content_hash` を含む) を送り、`sandbox_ready` を
   読む。attestation を固定の順で検査し (`verify_sandbox_ready`)、受信側に余分な
   行が無いこと、子の全 task の `/proc` status を確かめてから `{"op": "load"}` を
   送る。どれかに通らなければ load を送らずに kill し `sandbox_unavailable`。
3. `plugin_ready` を読む。起動の deadline は handshake の直前に 1 つだけ作り、
   ここまでの全段で共有する。

分類は「親が load を送ったか」で分ける。load 前の失敗・timeout・SIGSYS は
`sandbox_unavailable` (reason 別)、load 後の SIGSYS (親 kill なし) は原因未確定の
`crashed / sigsys_unattributed`。

**load の後に worker から届く行は、すべて候補が制御し得る入力として扱う**
(plugin は worker と同じ process で protocol fd へ直接書ける)。親は次を自分で
担保し、worker 側の整形に頼らない: パースの例外はどれも protocol 違反 (NaN・
重複キーも拒否)、要求ごとの乱数 id と応答の照合、要求の前に届いていた行と
1 要求への複数行の拒否、死を観測した worker の残した行の不採用、応答の `pid` は
`Popen.pid` との照合だけ、戻り値は kind 別に親で検証 (indicator の系列長は親が
渡した df の index で)、worker 由来の文字列は `untrusted_text` を通してから例外・
ログへ出す。request id は応答の出所の証明ではなく (plugin も id を観測できる)、
ずれと先回りを排除するためのもの。

- request (call() のたびに親が送る 1 行): `{"df": <df wire>, "params": {...}, "id": "<hex>"}`
  - df wire (DataFrame の JSON 安全な表現。**index は UTC の ISO8601
    文字列**、カラムは OHLCV の 5 本固定):
    `{"index": [iso8601 str, ...], "open": [...], "high": [...],
      "low": [...], "close": [...], "volume": [...]}`
    float は Python の json エンコード/デコードが shortest-roundtrip
    表現 (repr アルゴリズム) であるため、往復させても bit-exact に一致
    する — 精度劣化を心配してカスタムシリアライザを書く必要はない。
- response (request 1 件につき 1 行):
  `{"ok": true, "result": <kind別 dict/list>, "pid": int, "id": <要求の id>}` または
  `{"ok": false, "error": "<message>", "pid": int, "id": <要求の id>}`
  — kind 別のスキーマ検証・harness 専有キー (`bar_ts`) の拒否は **すべて
  この親プロセス側 (sandbox.py) で行う**。

**タイムアウト・上限超過はセッションを使用不能にする**: `call()` が
timeout/出力上限超過/worker 予期せぬ終了のいずれかに遭遇したら、その
セッションは即座に kill され、以後の `call()` は即座に `SandboxError`
を送出する (再利用不可 — 「壊れたセッションで次の call が謎の挙動をする」
を構造的に防ぐ)。一方、plugin コード自身が例外を投げた・スキーマ検証に
失敗した (worker は生きたまま) 場合はセッションは生存し続け、次の
`call()` も通常どおり使える — これは「timeout/kill」とは別の分類。
"""
from __future__ import annotations

import ast
import ctypes
import errno
import json
import math
import os
import platform
import secrets
import select
import signal
import subprocess
import sys
import threading
import tempfile
import time
import logging
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentic_fx.core import landlock, runtime_fingerprint, seccomp
from agentic_fx.core.contracts import (
    Direction, EntryType, StrategyAction, StrategyDecision,
)
from agentic_fx.core.plugin_files import PluginFileTooLarge
from agentic_fx.plugin.loader import PluginMeta, content_hash
from agentic_fx.plugin.worker_isolation import (
    ATTESTED_FIELDS, CANDIDATE_ISOLATION_REASONS, WORKER_SANDBOX_REASONS)

if TYPE_CHECKING:
    import pandas as pd

    from agentic_fx.config import PluginSettings
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet


class SandboxError(Exception):
    """plugin コード実行 (AST 検査・起動・呼び出し・戻り値検証) に起因
    する全エラーの単一表現。呼び出し側はこれ 1 種類だけを catch すれば
    よい (実装内訳を漏らさない)。"""

    def __init__(self, message: str, *, code: str = "backtest_failed",
                 sandbox_reason: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        # `sandbox_unavailable` の固定 reason、または SIGSYS による `crashed` の
        # `sigsys_unattributed`。技術ログと activity にだけ出す (例外文字列には入れない)
        self.sandbox_reason = sandbox_reason


# --- check_source ----------------------------------------------------

# import allowlist。ルートモジュール名のみで判定する (`numpy.random` は
# `numpy` として許可域に入る — サブモジュール個別の遮断は denylist の
# 役目)。`__future__` は実行時にモジュールを import するのではなくコン
# パイラへの指示 (PEP 236) であり、I/O も動的呼び出しも行えない — plugin
# 作者 (LLM/人間問わず) が `from __future__ import annotations` を書く
# ことは型注釈のベストプラクティスとして極めて一般的なため、これを拒否
# すると善意の plugin の大半が書けなくなる。brief の逐語リストに無い
# 拡張だが、脅威モデル上安全なので明示的に allowlist へ加える。
_ALLOWED_IMPORT_ROOTS = frozenset({"math", "statistics", "numpy", "pandas",
                                   "__future__"})

# import として個別に禁止するドット付きパス (allowlist のルートに含まれ
# ていても、このプレフィックスに一致するサブモジュールの import は拒否)。
_DENY_IMPORT_PREFIXES = ("pandas.io", "numpy.lib.npyio")

# 名前 (Name の id、または Attribute の attr) として出現したら拒否する
# トークン集合 (codex R1 C6 の逐語リスト + レビュー fix round 1 F4:
# ExcelWriter/HDFStore (I/O 用クラス)・dump/dumps (ndarray の pickle 系
# シリアライズメソッド、loads はあったが dump/dumps が抜けていた) +
# 最終レビュー F5: importorskip (`pytest.importorskip("os")` は許可済み
# `pytest` の属性呼び出しとして import allowlist/denylist の双方を素通り
# し、submit 実行だけで任意モジュールを import できてしまう迂回経路
# だった — Attribute (`pytest.importorskip`) にも裸 Name (`from pytest
# import importorskip` 経由の束縛) にも同じ判定がかかる既存機構にトークン
# を 1 つ追加するだけで塞げる)。
_DENY_NAMES = frozenset({
    "open", "eval", "exec", "compile", "__import__", "input", "breakpoint",
    "globals", "getattr", "setattr", "delattr", "vars",
    "load", "loads", "loadtxt", "genfromtxt", "save", "savetxt", "savez",
    "memmap", "fromfile", "tofile", "pickle", "unpickle", "dump", "dumps",
    "ExcelWriter", "HDFStore", "importorskip",
    "capsys", "capfd", "capsysbinary", "capfdbinary",
    # [indicator-consumption-wiring] §2.4 (codex r3 I7): strategy と
    # indicator が同一 worker プロセスを共有するため、pandas/numpy の
    # **プロセス全体のグローバル状態**を書き換える API を遮断する
    # (`np.errstate` / `pd.option_context` は with 脱出で復元するので
    # 足さない)。`_is_denied_bare_name` は `ast.Attribute.attr` にも
    # 効くので `pd.set_option(...)` の形も拒否できる。
    "set_option", "reset_option", "set_eng_float_format",
    "seterr", "seterrcall", "setbufsize", "set_printoptions",
})

# `to_` 前綴りの属性は既定で禁止 (`to_csv`/`to_pickle`/`to_sql` 等の I/O
# 系メソッドを想定) だが、純粋な型変換であるこの 4 つだけは許可する。
_TO_ALLOWED = frozenset({"to_dict", "to_list", "to_numpy", "to_pydatetime"})


def _is_denied_bare_name(name: str) -> bool:
    """`read_`/`to_` 接頭辞規則 + 固定 denylist 集合を 1 箇所に統一した
    判定 (レビュー fix round 1 F1)。**`ast.Attribute.attr`・`ast.Name.id`
    (Store/Load 問わず)・`ast.FunctionDef`/`ast.AsyncFunctionDef` の関数
    定義名・`ImportFrom` で実際に import される名前 (`alias.name`、
    `asname` の有無に関わらず) の 4 箇所すべてに同じ判定を適用する** —
    以前は `ast.Attribute` にしか及んでおらず、`from pandas import
    read_csv` のような from-import 経由の別名バイパスや、plugin が
    `def read_xxx(...): ...` のような名前の関数を自ら定義して
    (`ast.Attribute` を経由しない直接呼び出しで) denylist を素通りする
    経路が残っていた (codex+sonnet 並行レビュー F1, Critical)。
    """
    if name.startswith("read_"):
        return True
    if name.startswith("to_") and name not in _TO_ALLOWED:
        return True
    return name in _DENY_NAMES


def check_source(path: Path, *, extra_allowed: frozenset[str] = frozenset()) -> None:
    """plugin.py (または test_plugin.py) を AST 検査し、denylist/allowlist
    に反する場合は `SandboxError` を送出する。反しなければ何も返さない。

    構文名ベースの検査であり動的呼び出しは防げない (モジュール docstring
    の脅威モデルを参照)。
    """
    allowed_roots = _ALLOWED_IMPORT_ROOTS | extra_allowed
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (SyntaxError, UnicodeDecodeError, OSError) as exc:
        raise SandboxError(f"{path}: cannot parse source ({exc})") from exc

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_import_name(alias.name, allowed_roots, path)
        elif isinstance(node, ast.ImportFrom):
            _check_import_from(node, allowed_roots, path)
        elif isinstance(node, ast.Name):
            if _is_denied_bare_name(node.id):
                raise SandboxError(f"{path}: use of name {node.id!r} is not allowed")
        elif isinstance(node, ast.arg):
            if _is_denied_bare_name(node.arg):
                raise SandboxError(f"{path}: argument {node.arg!r} is not allowed")
        elif isinstance(node, ast.Attribute):
            if _is_denied_bare_name(node.attr):
                raise SandboxError(f"{path}: attribute {node.attr!r} is not allowed")
            # [indicator-consumption-wiring] §2.4 (codex r4 I7): 外部
            # オブジェクトの属性への**代入・削除**は構文種別
            # (Assign / AugAssign / AnnAssign / タプル target / for target /
            # del / with ... as) によらず一律 reject する。`ctx` を見れば
            # 構文種別を列挙せずに全形を捕まえられる (現行 example に
            # 外部属性代入の正当用途は無い)。
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                raise SandboxError(
                    f"{path}: attribute assignment/deletion "
                    f"({node.attr!r}) is not allowed")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if _is_denied_bare_name(node.name):
                raise SandboxError(
                    f"{path}: defining a function named {node.name!r} is not allowed")
        elif isinstance(node, ast.Global):
            raise SandboxError(f"{path}: 'global' statement is not allowed")


def _check_import_name(name: str, allowed_roots: frozenset[str], path: Path) -> None:
    if not name:
        raise SandboxError(f"{path}: relative import is not allowed")
    _check_deny_prefix(name, path)
    root = name.split(".")[0]
    if root not in allowed_roots:
        raise SandboxError(
            f"{path}: import of {name!r} is not allowed "
            f"(allowed roots: {sorted(allowed_roots)})")


def _check_import_from(node: ast.ImportFrom, allowed_roots: frozenset[str],
                       path: Path) -> None:
    module = node.module or ""
    if not module:
        raise SandboxError(f"{path}: relative import is not allowed")
    _check_import_name(module, allowed_roots, path)
    for alias in node.names:
        full = f"{module}.{alias.name}"
        _check_deny_prefix(full, path)
        # F1: from-import の別名バイパス封鎖 — `from pandas import
        # read_csv` や `from pandas import read_csv as rc` のように
        # import される**元の**名前 (alias.name、asname は無視) 自体が
        # denylist に触れるなら、そのモジュールが allowlist に載っていて
        # ドット付きパスが _DENY_IMPORT_PREFIXES に一致しなくても拒否する。
        if _is_denied_bare_name(alias.name):
            raise SandboxError(
                f"{path}: import of {module}.{alias.name} is not allowed")


def _check_deny_prefix(dotted: str, path: Path) -> None:
    for prefix in _DENY_IMPORT_PREFIXES:
        if dotted == prefix or dotted.startswith(prefix + "."):
            raise SandboxError(f"{path}: import of {dotted!r} is not allowed")


# --- env builder -------------------------------------------------------

# worker.py の import 前に BLAS/OpenMP 系ライブラリを強制的にシングル
# スレッド化する。理由は 2 つ: ①RLIMIT_AS=512MB は「仮想アドレス空間」
# の上限であり、OpenBLAS はコア数に比例したスレッド分の仮想メモリ領域を
# 事前確保する — マルチコア機で 512MB を容易に超え `import pandas` 自体
# が失敗する (実測で再現・本番の CPU 台数に非依存にするため固定で潰す)。
# ②結果としてスレッド/プロセス生成そのものが減り、RLIMIT_NPROC の
# 見積りも単純な固定値で足りるようになる。
_SINGLE_THREAD_ENV = {
    "OPENBLAS_NUM_THREADS": "1",
    "OMP_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "VECLIB_MAXIMUM_THREADS": "1",
    "NUMBA_NUM_THREADS": "1",
}

# worker.py の src ディレクトリ (sandbox.py の 2 階層上 — src/agentic_fx/
# plugin/sandbox.py → src/agentic_fx/plugin → src/agentic_fx → src)。
_SRC_DIR = Path(__file__).resolve().parents[2]


def _build_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """子プロセスに渡す env を最小構築する (単体テストで直接検証可能な
    純関数として切り出す — brief 「構築関数を単体テストで直接検証」)。

    継承するのは `PATH` のみ (実行ファイル解決に必要)。それ以外の親 env
    変数は — API キー等の秘密情報を含みうる `AFX_*` を含め — 一切継承
    しない (fail closed)。`PYTHONPATH` は本パッケージの src ディレクト
    リを明示的に 1 つだけ設定する (親の PYTHONPATH を継承・合成しない)。
    `PYTHONSAFEPATH=1` は `-m` 起動時に cwd (= plugin フォルダ、攻撃者が
    書いた `plugin.py` と同じ場所) が `sys.path` の先頭に挿入されるのを
    防ぐ — plugin フォルダに `json.py`/`numpy.py` のような stdlib 名を
    衝突させて worker 自身の import を乗っ取る fail-open 経路を塞ぐ
    (PEP 706, Python 3.11+)。
    """
    base = base_env if base_env is not None else dict(os.environ)
    env = {"PATH": base.get("PATH", ""), "PYTHONPATH": str(_SRC_DIR),
           "PYTHONSAFEPATH": "1"}
    env.update(_SINGLE_THREAD_ENV)
    return env


# --- 隔離の admission と起動応答の検証 -------------------------------------

#: `sandbox_unavailable` の例外文字列 (固定。reason は `sandbox_reason` 属性と技術ログだけ)
SANDBOX_UNAVAILABLE_MESSAGE = (
    "plugin worker could not be started under the required isolation "
    "(sandbox unavailable)")
#: `load` 後の SIGSYS による `crashed` の例外文字列 (固定)
SIGSYS_CRASH_MESSAGE = (
    "plugin worker was killed by SIGSYS (a forbidden syscall or an external "
    "signal; cause not determined)")

# 代表自己試験の子が呼ぶ隔離の入口 (本番と同じ Landlock / seccomp を直接掛ける。
# PluginSession は経由しない)
_SELFTEST_HOOK = "agentic_fx.plugin.worker_isolation:isolate_for_selftest"

# Popen 直前に読む、現在 thread の status
_THREAD_STATUS_PATH = "/proc/thread-self/status"

# 子の全 task に期待する status (継承 filter は事前に拒否するので filter は 1 本固定)
_EXPECTED_TASK_STATUS = (("NoNewPrivs", "1"), ("Seccomp", "2"), ("Seccomp_filters", "1"))

_ENVELOPE_FIELDS = ("phase", "ok", "pid")


@dataclass(frozen=True)
class AdmissionResult:
    """全 `PluginSession` 共通の admission の結果。`landlock_abi` は親が測った ABI
    (`plan_for_abi` の切り詰め後) で、成功 attestation の期待値に使う。"""

    admitted: bool
    reason: str | None
    warning: str | None
    landlock_abi: int | None
    supported: bool


def _run_selftest() -> runtime_fingerprint.SelftestOutcome:
    return runtime_fingerprint.run_representative_selftest(isolation_hook=_SELFTEST_HOOK)


# fingerprint ごとの判定 (集合外の自己試験は process 内で 1 回だけ)
_RUNTIME_ADMISSION = runtime_fingerprint.RuntimeAdmission(selftest=_run_selftest)
# この process の判定。fingerprint の計算 (数十 ms) を session ごとに繰り返さない
_ADMISSION_RESULT: AdmissionResult | None = None
_ADMISSION_LOCK = threading.Lock()


def host_preflight() -> tuple[str | None, int]:
    """x86_64、Landlock ABI 3 以上、seccomp (TSYNC・LOG・KILL_PROCESS・ERRNO) を測る。
    使えなければ固定 reason、使えれば (None, 切り詰め後の ABI)。"""
    if platform.machine() != "x86_64":
        return "arch_unsupported", 0
    abi = landlock.landlock_abi()
    if abi <= 0:
        return "landlock_unavailable", 0
    if abi < landlock.PLUGIN_MIN_ABI:
        return "landlock_abi_too_old", abi
    if not seccomp.probe_support().available:
        return "seccomp_unavailable", abi
    missing = landlock.missing_required_system_dirs()
    if missing:
        # zoneinfo 等が無いと worker は fd_open_failed で落ち、同じ allowlist の
        # 自己試験も落ちて誤解を招く reason になる。ここで明確に止める
        return f"missing_system_dir:{missing[0].name}", abi
    if _seccomp_status_field_missing():
        return "seccomp_status_field_unavailable", abi
    return None, landlock.plan_for_abi(abi).abi


def _seccomp_status_field_missing() -> bool:
    """この kernel が `/proc/<pid>/status` に `Seccomp_filters` 欄を出すかを調べる。
    欄の無い kernel では worker の継承 filter 検査が全 session を
    `inherited_seccomp_filter` にするので、host preflight で先に検知する。自分の
    status すら読めない host では判断を保留し、Popen 直前の継承検査に委ねる。"""
    try:
        with open(_THREAD_STATUS_PATH, "rb") as f:
            data = f.read(65536)
    except OSError:
        return False
    return seccomp.seccomp_filters_field(data) is None


def runtime_admission() -> AdmissionResult:
    """全 `PluginSession` が起動前に通る共通の admission。

    host の要求水準 → runtime fingerprint → (集合外なら) 代表自己試験の順で判定し、
    結果を process 内に保持する。service は起動時に呼び (eager)、その他の入口は最初の
    session で呼ぶ (lazy)。fingerprint を計算できないときも自己試験失敗と同じ固定
    reason にする (fail closed)。
    """
    global _ADMISSION_RESULT
    with _ADMISSION_LOCK:
        cached = _ADMISSION_RESULT
        if cached is not None:
            return cached
        reason, abi = host_preflight()
        # host preflight の失敗 (arch・ABI・zoneinfo 等) は恒久。fingerprint が作れない
        # ・一時的な selftest 失敗は恒久 cache せず、評価ごとに再判定させる (RuntimeAdmission
        # 側が間隔を空けて selftest を再試行する)
        cacheable = True
        if reason is not None:
            result = AdmissionResult(False, reason, None, None, False)
        else:
            try:
                fingerprint = runtime_fingerprint.compute_fingerprint(landlock_abi=abi)
                decision = _RUNTIME_ADMISSION.admit(fingerprint)
            except Exception:  # noqa: BLE001  fingerprint が作れない環境は集合外の失敗扱い
                _log.warning("plugin sandbox: runtime fingerprint could not be computed",
                             exc_info=True)
                decision = None
                cacheable = False  # 一時要因 (fd・メモリ逼迫) かもしれないので恒久化しない
            if decision is not None and decision.admitted:
                result = AdmissionResult(True, None, decision.warning, abi, decision.supported)
                if decision.warning:
                    _log.warning("plugin sandbox: %s", decision.warning)
            else:
                result = AdmissionResult(False, runtime_fingerprint.SELFTEST_FAILED_REASON,
                                         None, None, False)
                if decision is not None and decision.retryable:
                    cacheable = False
        if cacheable:
            _ADMISSION_RESULT = result
        return result


def inherited_filter_preflight() -> str | None:
    """`Popen` の直前に、現在 thread の `Seccomp_filters` が 0 であることを確かめる。

    field が無い・読めない・0 でないときは `inherited_seccomp_filter`。継承した filter
    を持つ親から起こした worker は attestation の `Seccomp_filters=1` を満たせず、
    filter の合成もしないので、CPython を起動する前にここで止める。
    """
    try:
        with open(_THREAD_STATUS_PATH, "rb") as f:
            data = f.read(65536)
    except OSError:
        return "inherited_seccomp_filter"
    for line in data.split(b"\n"):
        if line.startswith(b"Seccomp_filters:"):
            if line.split(b":", 1)[1].strip() == b"0":
                return None
            return "inherited_seccomp_filter"
    return "inherited_seccomp_filter"


def expected_attestation(landlock_abi: int, nonce: str) -> dict[str, object]:
    """成功 `sandbox_ready` の attested field の期待値。すべて親が自分で決める
    (worker の申告から作らない)。"""
    plan = landlock.plan_for_abi(landlock_abi)
    values = {
        "sandbox_profile_version": runtime_fingerprint.SANDBOX_PROFILE_VERSION,
        "landlock_fs_abi": plan.abi,
        "landlock_tsync": plan.tsync,
        "seccomp": seccomp.SECCOMP_PROFILE,
        "keyring": "anonymous",
        "network": plan.network,
        "scope": plan.scope,
        "nonce": nonce,
    }
    return {k: values[k] for k in ATTESTED_FIELDS}


def _same_value(actual: object, expected: object) -> bool:
    # True == 1 のような型の違う一致を許さない
    return type(actual) is type(expected) and actual == expected


def verify_sandbox_ready(message: dict[str, Any], *, expected: dict[str, object],
                         popen_pid: int) -> str | None:
    """worker の 1 行目 (`sandbox_ready`) を検査し、拒否する reason を返す (通れば None)。

    検査の順は固定 (複数の違反があっても先の項目の reason になる):
    phase の有無 → phase の値 → `ok:false` → 未知 field → `pid` → attested field
    (表の順、`nonce` が最後) → field の並び順。
    """
    if "phase" not in message:
        return "attestation_missing"
    phase = message["phase"]
    if phase == "plugin_ready":
        return "attestation_order"
    if phase != "sandbox_ready":
        return "attestation_unexpected_message"
    ok = message.get("ok")
    if ok is False:
        reason = message.get("reason")
        if reason == "inherited_seccomp_filter":
            return reason
        if isinstance(reason, str) and reason in WORKER_SANDBOX_REASONS:
            return f"sandbox_setup_failed:{reason}"
        return "sandbox_setup_failed:unknown"
    if ok is not True:
        return "attestation_unexpected_message"
    allowed = set(_ENVELOPE_FIELDS) | set(ATTESTED_FIELDS)
    if any(key not in allowed for key in message):
        return "attestation_unexpected_field"
    if "pid" not in message:
        return "attestation_missing:pid"
    if not _same_value(message["pid"], int(popen_pid)):
        return "attestation_mismatch:pid"
    for name in ATTESTED_FIELDS:
        if name not in message:
            return f"attestation_missing:{name}"
        if not _same_value(message[name], expected[name]):
            return f"attestation_mismatch:{name}"
    order = (*_ENVELOPE_FIELDS, *ATTESTED_FIELDS)
    for actual, wanted in zip(message, order):
        if actual != wanted:
            if wanted == "pid" or wanted in ATTESTED_FIELDS:
                return f"attestation_mismatch:{wanted}"
            return "attestation_unexpected_field"
    return None


def check_worker_tasks(pid: int, *, proc_root: str = "/proc") -> str | None:
    """`pid` 配下の全 task の status が NoNewPrivs 1 / Seccomp 2 / Seccomp_filters 1
    であることを確かめる。読めない・field が無いときは `proc_status_unreadable`。"""
    task_dir = os.path.join(proc_root, str(int(pid)), "task")
    try:
        tids = sorted(os.listdir(task_dir))
    except OSError:
        return "proc_status_unreadable"
    if not tids:
        return "proc_status_unreadable"
    for tid in tids:
        try:
            with open(os.path.join(task_dir, tid, "status"), "rb") as f:
                data = f.read(65536)
        except OSError:
            return "proc_status_unreadable"
        fields: dict[str, str] = {}
        for line in data.decode("ascii", "replace").splitlines():
            key, sep, value = line.partition(":")
            if sep:
                fields[key] = value.strip()
        for name, wanted in _EXPECTED_TASK_STATUS:
            if name not in fields:
                return "proc_status_unreadable"
            if fields[name] != wanted:
                return f"proc_status_mismatch:{name}"
    return None


def startup_diagnostics() -> list[str]:
    """service の起動時に eager に回す診断。admission と seccomp の kernel log の読める
    状態を 1 行ずつ返す。失敗しても例外にはしない (service は起動し、評価ごとに
    fail closed する)。"""
    lines: list[str] = []
    try:
        result = runtime_admission()
        if result.admitted:
            lines.append(
                "plugin sandbox admission: ok "
                f"fingerprint={'supported' if result.supported else 'out_of_set_selftest_passed'} "
                f"landlock_abi={result.landlock_abi} seccomp={seccomp.SECCOMP_PROFILE} "
                f"profile={runtime_fingerprint.SANDBOX_PROFILE_VERSION}")
        else:
            lines.append(
                "plugin sandbox admission: unavailable "
                f"sandbox_reason={result.reason} — plugin の評価は行わない "
                "(signal・strategy・indicator の評価ごとに sandbox_unavailable になる)")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"plugin sandbox admission: check_failed ({type(exc).__name__})")
    try:
        lines.append(f"plugin sandbox kernel log: {seccomp.check_kernel_log()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"plugin sandbox kernel log: check_failed ({type(exc).__name__})")
    return lines


#: worker 由来の文字列をログ・例外・CLI へ出すときの上限 (文字数)
UNTRUSTED_TEXT_MAX_CHARS = 300


def untrusted_text(value: object, *, fallback: str = "unknown error") -> str:
    """worker (= plugin と同じ process) から来た文字列を、例外文字列・技術ログ・
    人間向け CLI へ出してよい形にする唯一の関数。

    str 以外や空は `fallback`。制御文字・書式文字・行区切り (Unicode の Cc・Cf・
    Zl・Zp) は取り除き、`UNTRUSTED_TEXT_MAX_CHARS` 文字で切る。
    """
    if not isinstance(value, str) or not value:
        return fallback
    kept: list[str] = []
    for ch in value:
        if unicodedata.category(ch) in {"Cc", "Cf", "Zl", "Zp"}:
            continue
        kept.append(ch)
        if len(kept) > UNTRUSTED_TEXT_MAX_CHARS:
            break
    text = "".join(kept[:UNTRUSTED_TEXT_MAX_CHARS])
    if len(kept) > UNTRUSTED_TEXT_MAX_CHARS:
        text += "...(truncated)"
    return text or fallback


class _ProtocolViolation(ValueError):
    """worker の行が protocol の形をしていない (パース不能・NaN・重複キー等)。"""


def _reject_constant(name: str) -> object:
    raise _ProtocolViolation(f"non-finite number {name} is not allowed")


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise _ProtocolViolation("duplicate key in worker response")
        out[key] = value
    return out


def parse_worker_line(payload: bytes) -> object:
    """worker の 1 行を JSON として読む。NaN・Infinity・重複キーは拒否する。

    load 後の行は plugin が protocol fd へ直接書き得るので、パーサが投げ得る例外
    (巨大な整数の ValueError、深い入れ子の RecursionError 等) はすべて
    `_ProtocolViolation` にまとめる。
    """
    try:
        return json.loads(payload, parse_constant=_reject_constant,
                          object_pairs_hook=_no_duplicate_keys)
    except _ProtocolViolation:
        raise
    except Exception as exc:  # noqa: BLE001  どの例外でも protocol 違反として扱う
        raise _ProtocolViolation(f"unparsable worker line ({type(exc).__name__})") from None


# --- PluginSession -------------------------------------------------------

_KIND_PAYLOAD_KEYS: dict[str, tuple[str, ...]] = {
    "indicator": ("df", "params"),
    "signal": ("df", "params"),
    "strategy": ("df", "params"),
}

# セッション起動 (handshake → ready 応答) を待つ固定タイムアウト。
# `sandbox_timeout_sec` (既定 10s、call() 用) とは別枠 — pandas の初回
# import はディスクキャッシュ次第で数秒かかることがあり、call() の
# timeout をここに使い回すと通常起動と暴走 plugin の判別ができなくなる。
_STARTUP_TIMEOUT_SEC = 30.0

# ready 応答の読み取り上限バイト数。`settings.sandbox_output_max_bytes`
# は「plugin コードが返す結果」向けのユーザー設定であり、worker 自身が
# 生成する固定形の起動応答 (数十バイト程度) にそれを流用すると、利用者が
# その値を小さく設定した途端に正常起動まで oversize 扱いになってしまう
# — 別の固定 (かつ十分に大きい) 上限を持つ。
_STARTUP_MAX_BYTES = 65536
_CPU_TOLERANCE_SEC = 0.05
_KILL_REAP_TIMEOUT_SEC = 5.0
# stdout が EOF になっても、worker の終了状態が wait4 で見えるまで数 ms 遅れる
# ことがある。ここで待たずに kill すると CPU 上限死が親 kill (crashed) に
# 化けるので、呼び出しの deadline を超えない範囲でこの時間だけ回収を待つ。
_EOF_REAP_GRACE_SEC = 1.0
_EOF_REAP_POLL_SEC = 0.002
_ORPHAN_CAPACITY = 64
_ORPHANS: list["PluginSession"] = []
_ORPHAN_OVERFLOW_LOGGED = False
_ORPHANS_LOCK = threading.RLock()
_log = logging.getLogger(__name__)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


# pidfd は数値の pid と違い、指す process が回収された後に番号が再利用されても別の
# process を指さない。この Python には os.pidfd_open / signal.pidfd_send_signal が
# 無い build があるので syscall を直接呼ぶ (番号は全 arch 共通の表、Linux 5.1+)。
_SYS_PIDFD_SEND_SIGNAL = 424
_SYS_PIDFD_OPEN = 434
# pidfd の指す pid を pgid とする process group 全体へ送る (Linux 6.9+)
_PIDFD_SIGNAL_PROCESS_GROUP = 1 << 2
_P_PIDFD = 3
_LIBC: Any = None


def _libc() -> Any:
    global _LIBC
    if _LIBC is None:
        lib = ctypes.CDLL(None, use_errno=True)
        lib.syscall.restype = ctypes.c_long
        _LIBC = lib
    return _LIBC


def _pidfd_open(pid: int) -> int:
    """pid の pidfd を返す。失敗は OSError (errno つき)。"""
    lib = _libc()
    ctypes.set_errno(0)
    fd = lib.syscall(ctypes.c_long(_SYS_PIDFD_OPEN), ctypes.c_int(pid), ctypes.c_uint(0))
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return int(fd)


def _pidfd_send_signal(pidfd: int, sig: int, flags: int = 0) -> None:
    """pidfd へ signal を送る。失敗は OSError (ESRCH は ProcessLookupError)。"""
    lib = _libc()
    ctypes.set_errno(0)
    rc = lib.syscall(ctypes.c_long(_SYS_PIDFD_SEND_SIGNAL), ctypes.c_int(pidfd),
                     ctypes.c_int(sig), ctypes.c_void_p(None), ctypes.c_uint(flags))
    if rc < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))


class _WriteDeadlineExpired(Exception):
    """worker への 1 行を deadline までに書き切れなかった (worker が stdin を読まない)。"""


def reap_orphans() -> None:
    """Keep unreaped children strongly referenced until the kernel releases them."""
    with _ORPHANS_LOCK:
        for session in list(_ORPHANS):
            if session._reap_worker():
                _ORPHANS.remove(session)
                _log.info("plugin worker orphan reaped pid=%s", session.pid)


class PluginSession:
    """1 plugin につき worker サブプロセスを 1 個だけ起動し、`call()` を
    繰り返し呼べるセッション。context manager として使う。
    """

    def __init__(self, meta: PluginMeta, *, settings: "PluginSettings",
                 resolved: "ResolvedIndicatorSet | None" = None) -> None:
        """`settings` は **`config.Settings` 全体ではなく `Settings.plugin`
        (`PluginSettings`) を渡す** — brief の記法 (`*, settings`) は
        アプリ全体設定を指しているとも読めるが、サンドボックスをアプリの
        設定スキーマ全体に結合させない (このモジュールが必要とするのは
        `plugin.*` の 5 キーのみ) ため、呼び出し側が `full_settings.plugin`
        を渡す形を fail-closed 側の設計判断として採用する (呼び出し側の
        取り違えは型で検出できる — `Settings` を渡すと属性アクセスで
        即座に `AttributeError` になる)。

        `resolved` は strategy kind で**必須** (依存なしでも
        `ResolvedIndicatorSet.empty(root)`)。indicator/signal は `None`。
        セッションは内部で再解決しない — 解決は composition root の責務
        (設計書 §2.3)。"""
        self._meta = meta
        self._settings = settings
        self._resolved = resolved
        reap_orphans()
        self._proc: subprocess.Popen | None = None
        self._stderr_file: Any | None = None
        self._dead = False
        self._cpu_sec: float | None = None
        self.pid: int | None = None
        self.worker_returncode: int | None = None
        self.worker_signal: int | None = None
        self.worker_cpu_sec: float | None = None
        self.parent_kill_sent = False
        self.worker_unreaped = False
        self._collected_elsewhere = False
        # Popen 直後に取る worker の pidfd。数値の pid / pgid は回収後に再利用され得る
        # ので、回収の後に signal を送るのはこれ経由だけにする。取れない環境では None
        self._pidfd: int | None = None
        # pidfd の使用と close を直列にする (別 thread の close が、閉じて番号が
        # 再利用された fd へ signal を送らないように)。中で他の lock を取らない
        self._pidfd_lock = threading.Lock()
        self._group_signal_unsupported = False
        self._leftover_kill_sent = False
        # __enter__・call・close の本体を直列にする。別 thread の close が、owner の
        # 読み書き中の fd を閉じて番号を再利用させないため
        self._lock = threading.RLock()
        self.error_code: str | None = None
        # `sandbox_unavailable` の固定 reason、または load 後の SIGSYS の
        # `sigsys_unattributed`。それ以外は None
        self.sandbox_reason: str | None = None
        # worker を起こしてから load を送るまでの間だけ True。この間の失敗は
        # plugin のコードが動き得なかった失敗 (`sandbox_unavailable`) になる
        self._awaiting_load = False
        # load を送った後、`plugin_ready` (load への正規の 1 応答) を読む間だけ True。
        # この 1 行は候補が死んでも読む (plugin_error を crashed に化けさせない)。
        # これ以降の call() 応答では False に戻し、死後の行は採用しない (§2.5)
        self._reading_load_response = False
        # graceful close (close op を送って応答を待つ) の間だけ True
        self._closing = False
        self._sigsys_reported = False
        self.stderr_tail: str | None = None
        self.stderr_unavailable = False
        self._stderr_unavailable_logged = False
        self._stdin_fd: int | None = None
        self._stdout_fd: int | None = None
        self._stdout_buffer = bytearray()
        self._started = False
        # __enter__ と call は construction したスレッドだけが呼ぶ。close は
        # 後始末の契約 (例外を出さない) を優先し、どの thread からでも受ける。
        self._owner_thread = threading.get_ident()

    @property
    def cpu_sec(self) -> float | None:
        """worker の累積 CPU 秒。値は親が `wait4` で回収したときの rusage
        (utime + stime) であり、worker 自己申告の close 応答ではない。
        **`close()` 完了後に確定する**。正常終了・plugin error 後・worker の
        自死後は float。**`None` になるのは** 親が SIGKILL を送った場合、
        worker を回収できなかった (unreaped) 場合、他所で先に回収されて
        rusage が得られなかった場合、`__enter__` 失敗で worker が
        起動しなかった場合、まだ終了していない場合。"""
        return self._cpu_sec

    def _check_owner_thread(self) -> None:
        current = threading.get_ident()
        if current != self._owner_thread:
            raise RuntimeError(
                f"PluginSession used from a different thread than its "
                f"owner thread (owner={self._owner_thread}, "
                f"current={current}) — PluginSession is single-thread-owned")

    def __enter__(self) -> "PluginSession":
        """起動シーケンス全体を 1 つの try/except で包む (`__enter__` が例外を
        投げると `__exit__` は呼ばれないので、ここで必ず `close()` を通してから
        `SandboxError` として投げ直す)。

        worker を起こす前に `loader.content_hash` (上限つきの読み) で plugin を
        再計算して discovery 時の hash と照合し、`check_source` を通す。worker には
        同じ hash を渡し、worker は自分が読んだ bytes をその hash と照合してから
        exec する。その後の手順はモジュール docstring の「起動は二段」を参照。"""
        self._check_owner_thread()
        with self._lock:
            return self._enter_locked()

    def _enter_locked(self) -> "PluginSession":
        if self._started:
            raise SandboxError("plugin session cannot be reused")
        self._started = True
        try:
            try:
                current_hash = content_hash(self._meta.path)
            except PluginFileTooLarge as exc:
                raise SandboxError(f"plugin {self._meta.name!r}: {exc.reason}",
                                   code="plugin_error") from exc
            if current_hash != self._meta.content_hash:
                raise SandboxError(
                    f"plugin {self._meta.name!r}: content changed since "
                    "discovery (hash mismatch) — refusing to execute "
                    f"(expected {self._meta.content_hash}, got {current_hash})")
            check_source(self._meta.path / "plugin.py")

            if self._meta.kind == "strategy" and self._resolved is None:
                raise SandboxError(
                    f"plugin {self._meta.name!r}: strategy session requires "
                    "a resolved indicator set (resolved=...)")
            # 逆向きの契約: indicator/signal は `resolved` を受け取らない。受け取ると
            # 依存情報 (indicator の実体パス等) が handshake に混入し得る。
            if self._meta.kind != "strategy" and self._resolved is not None:
                raise SandboxError(
                    f"plugin {self._meta.name!r}: kind={self._meta.kind!r} "
                    "session must not receive a resolved indicator set "
                    "(resolved= is strategy-only)")
            indicator_specs: list[dict] = []
            if self._resolved is not None:
                inventory_root = Path(self._resolved.inventory_root).resolve()
                for item in self._resolved.items:
                    real = Path(item.plugin_py).resolve()
                    try:
                        real.relative_to(inventory_root)
                    except ValueError:
                        raise SandboxError(
                            f"indicator {item.plugin_name!r} resolves outside "
                            f"inventory_root ({real})") from None
                    try:
                        current = content_hash(real.parent)
                    except PluginFileTooLarge as exc:
                        raise SandboxError(
                            f"indicator {item.plugin_name!r}: {exc.reason}",
                            code="plugin_error") from exc
                    if current != item.content_hash:
                        raise SandboxError(
                            f"indicator {item.plugin_name!r}: content changed "
                            "since resolution (hash mismatch) — refusing to "
                            f"execute (expected {item.content_hash}, got {current})")
                    check_source(real)
                indicator_specs = self._resolved.handshake_items()
                # 検査済みの実体パスと、worker が読んだ bytes と照合する hash を載せる
                for spec, item in zip(indicator_specs, self._resolved.items):
                    spec["plugin_py"] = str(Path(item.plugin_py).resolve())
                    spec["content_hash"] = item.content_hash

            # 隔離を準備できない host・runtime では plugin を一切起動しない
            admission = runtime_admission()
            if not admission.admitted:
                raise self._unavailable(admission.reason or "runtime_fingerprint_selftest_failed")
            assert admission.landlock_abi is not None

            env = _build_env()
            try:
                self._stderr_file = tempfile.TemporaryFile(mode="w+b")
                stderr: Any = self._stderr_file
            except OSError:
                self.stderr_unavailable = True
                stderr = subprocess.DEVNULL
            # これ以降 load を送るまでの失敗は、plugin が動き得なかった失敗として分類する
            self._awaiting_load = True
            reason = inherited_filter_preflight()
            if reason is not None:
                raise self._unavailable(reason)
            try:
                # -B: sandbox 下の遅延 import が __pycache__ を作ろうとしないように
                self._proc = subprocess.Popen(
                    [sys.executable, "-B", "-m", "agentic_fx.plugin.worker",
                     str(self._meta.path)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=stderr, cwd=str(self._meta.path), env=env,
                    start_new_session=True, bufsize=0,
                )
            except Exception as exc:  # noqa: BLE001  起動自体の失敗は隔離の手前の失敗
                _log.warning("plugin worker spawn failed plugin=%s (%s)",
                             self._meta.name, type(exc).__name__)
                raise self._unavailable("worker_bootstrap_failed") from exc
            self.pid = self._proc.pid
            self._pidfd = self._open_child_pidfd(self._proc.pid)
            self._ensure_pipe_ownership()
            nonce = secrets.token_hex(16)
            handshake = {
                "cpu_sec": self._settings.sandbox_session_cpu_sec,
                "memory_mb": self._settings.sandbox_memory_mb,
                "nofile": self._settings.sandbox_nofile,
                "fsize_mb": self._settings.sandbox_fsize_mb,
                "attest_nonce": nonce,
                "content_hash": self._meta.content_hash,
                "kind": self._meta.kind,
                "indicators": indicator_specs,
            }
            # `outputs` は kind == "indicator" のときだけ載せる契約 (strategy / signal に
            # null のキーを届けない)。
            if self._meta.kind == "indicator":
                handshake["outputs"] = (list(self._meta.outputs)
                                        if self._meta.outputs is not None
                                        else None)
            # 起動の deadline は 1 つだけ作り、sandbox_ready の読取・検証・load の送信・
            # plugin_ready の読取で共有する
            deadline = time.monotonic() + _STARTUP_TIMEOUT_SEC
            self._send_startup_line(handshake, deadline)
            ready = self._read_response(_remaining(deadline), _STARTUP_MAX_BYTES)
            reason = verify_sandbox_ready(
                ready, expected=expected_attestation(admission.landlock_abi, nonce),
                popen_pid=self._proc.pid)
            if reason is None and self._stdout_buffer:
                reason = "attestation_unexpected_message"
            if reason is None:
                reason = check_worker_tasks(self._proc.pid)
            if reason is not None:
                self._refuse_startup(reason)
            if time.monotonic() >= deadline:
                self._refuse_startup("sandbox_startup_timeout")
            self._send_startup_line({"op": "load"}, deadline)
            self._awaiting_load = False
            # plugin_ready は load への正規の 1 応答。候補が ok:false を書いて
            # すぐ死に、親が遅れて死を先に観測しても、この 1 行は読んで分類する
            self._reading_load_response = True
            try:
                loaded = self._read_response(_remaining(deadline), _STARTUP_MAX_BYTES)
            finally:
                self._reading_load_response = False
            if loaded.get("ok") is False:
                self.error_code = "plugin_error"
                raise SandboxError(
                    "plugin worker failed to start: "
                    + untrusted_text(loaded.get("error"), fallback="unknown error"),
                    code="plugin_error")
            if loaded.get("ok") is not True:
                self._protocol_failure("plugin worker returned an invalid startup response")
            if loaded.get("phase") != "plugin_ready":
                # load の後でも、plugin_ready で始まらない応答は隔離の検証に通っていない
                self._dead = True
                self._kill()
                if self.worker_unreaped:
                    raise SandboxError("plugin worker could not be reaped", code="crashed")
                raise self._unavailable("attestation_order")
            if self.worker_returncode is not None:
                # plugin_ready ok:true を書いた直後に worker が死んでいた
                # (drain 経由で読んだ)。load 後の死として分類する
                raise self._worker_error("plugin worker exited after plugin_ready")
            return self
        except Exception as exc:
            self._dead = True
            self._awaiting_load = False
            requested_code = exc.code if isinstance(exc, SandboxError) else "backtest_failed"
            requested_reason = (exc.sandbox_reason if isinstance(exc, SandboxError)
                                else None)
            self.error_code = requested_code
            self.sandbox_reason = requested_reason
            self.close()
            final_code = "crashed" if self.worker_unreaped else requested_code
            final_reason = requested_reason if final_code == requested_code else None
            self.error_code = final_code
            self.sandbox_reason = final_reason
            if isinstance(exc, SandboxError):
                raise SandboxError(str(exc), code=final_code,
                                   sandbox_reason=final_reason) from exc
            raise SandboxError(f"failed to start plugin worker: {exc}",
                               code=final_code) from exc

    def _unavailable(self, reason: str) -> SandboxError:
        """`sandbox_unavailable` の例外を作り、session にも分類を記録する。"""
        self.error_code = "sandbox_unavailable"
        self.sandbox_reason = reason
        return SandboxError(SANDBOX_UNAVAILABLE_MESSAGE, code="sandbox_unavailable",
                            sandbox_reason=reason)

    def _refuse_startup(self, reason: str) -> None:
        """起動応答の検証に通らなかった worker を、load を送らずに止める。戻らない。"""
        self._dead = True
        self._kill()
        if self.worker_unreaped:
            raise SandboxError("plugin worker could not be reaped", code="crashed")
        raw = (reason.split(":", 1)[1]
               if reason.startswith("sandbox_setup_failed:") else reason)
        if raw in CANDIDATE_ISOLATION_REASONS:
            # 候補の plugin.py / config.yaml が原因 (symlink・非通常ファイル・guarded
            # dir)。環境障害ではなく候補の責任なので plugin_error にする
            # reason は例外文字列に出さず技術ログにだけ出す (§5.1)
            _log.warning("plugin worker refused candidate files plugin=%s reason=%s",
                         self._meta.name, raw)
            self.error_code = "plugin_error"
            raise SandboxError(
                "candidate plugin files could not be read under isolation",
                code="plugin_error")
        raise self._unavailable(reason)

    def _send_startup_line(self, obj: dict[str, Any], deadline: float) -> None:
        """起動中の 1 行を起動の deadline の中で送る。書けなければ worker の終了を
        待って分類する。書き切れないまま期限が切れたら、読取の期限切れと同じく
        `sandbox_startup_timeout` (load をまだ送り切っていないので)。"""
        try:
            self._write_line(obj, deadline=deadline)
        except _WriteDeadlineExpired:
            raise self._deadline_expired(_STARTUP_TIMEOUT_SEC) from None
        except OSError as exc:
            self._dead = True
            self._await_reap_grace(deadline)
            self._kill()
            if self.worker_unreaped:
                raise SandboxError("plugin worker could not be reaped",
                                   code="crashed") from exc
            raise self._worker_error(f"failed to write to plugin worker: {exc}") from exc

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        """graceful close (設計書 §2.4): worker が生きていれば
        `{"op": "close"}` を送り `{"ok": true, "cpu_sec": ...}` を
        `sandbox_timeout_sec` 以内で待つ。応答が来れば `cpu_sec` が確定し、
        来なければ従来どおり SIGKILL (`cpu_sec` は None のまま)。

        どの thread から呼ばれても例外を出さず、kill・reap・fd の close まで行う。
        owner 以外の thread からの close は、pidfd があれば owner が call の途中でも
        待たされないよう先に worker を kill してから (このとき graceful close は
        成立しない)、owner の読み書きが終わるのを lock で待って後始末する。"""
        try:
            if threading.get_ident() != self._owner_thread:
                self._interrupt_from_other_thread()
            with self._lock:
                self._close_locked()
        except Exception:  # noqa: BLE001  close は例外を出さない
            _log.warning("plugin worker close failed plugin=%s", self._meta.name,
                         exc_info=True)
        finally:
            self._close_pidfd()

    def _close_locked(self) -> None:
        if self.worker_unreaped:
            self._close_parent_fds()
            self._close_stderr()
            return
        proc = self._proc
        if proc is None:
            self._close_stderr()
            return
        try:
            if not self._reap_worker():
                if not self._dead:
                    self._closing = True
                    try:
                        self._graceful_close()
                    except Exception:  # noqa: BLE001
                        # timeout・EOF・書き込み失敗・壊れた応答のどれでも、下の
                        # kill へ進む (close は例外を投げない)
                        pass
                    finally:
                        self._closing = False
                if not self._reap_worker():
                    self._kill()
        except Exception:  # noqa: BLE001  後始末を飛ばさない
            _log.warning("plugin worker close failed plugin=%s; killing",
                         self._meta.name, exc_info=True)
            try:
                self._kill()
            except Exception:  # noqa: BLE001
                pass
        # stdout を読んだ owner thread だけがここで stream を閉じる。
        # kill が効かない場合も close は待機せず、この終端化だけで完了する。
        self._close_parent_fds()
        # An unreaped child must remain strongly reachable for a later
        # WNOHANG attempt, but none of the parent's pipe/file descriptors are
        # needed for that attempt.  Keeping them open here leaks an fd for
        # every stopped worker and, more importantly, prevents close() from
        # being a terminal operation from the caller's point of view.
        self._close_stderr()
        if not self.worker_unreaped:
            self._proc = None

    def _graceful_close(self) -> None:
        self._check_no_stray_output()
        request_id = secrets.token_hex(8)
        # close の要求と応答の待ちで 1 つの deadline を共有する
        deadline = time.monotonic() + self._settings.sandbox_timeout_sec
        self._write_line({"op": "close", "id": request_id}, deadline=deadline)
        response = self._read_response(_remaining(deadline),
                                       _STARTUP_MAX_BYTES)
        self._check_response_envelope(response, request_id,
                                      allowed=frozenset({"ok", "cpu_sec", "pid", "id"}))
        close_deadline = time.monotonic() + self._settings.sandbox_timeout_sec
        while not self._reap_worker() and time.monotonic() < close_deadline:
            time.sleep(0.01)

    def _check_no_stray_output(self) -> None:
        """要求を送る前に、受信側に何も残っていないことを確かめる。

        load 後の worker は plugin のコードと同じ process で、plugin は protocol fd へ
        直接書ける。要求より先に届いた行・bytes は次の応答として使わず protocol 違反に
        する (先回りの応答を排除する)。"""
        if self._proc is not None:
            self._ensure_pipe_ownership()
        fd = self._stdout_fd
        if fd is not None and not self._stdout_buffer:
            poller = select.poll()
            poller.register(fd, select.POLLIN)
            try:
                if poller.poll(0):
                    chunk = os.read(fd, 65536)
                    self._stdout_buffer.extend(chunk)
            except OSError:
                pass
        if self._stdout_buffer:
            self._stdout_buffer.clear()
            self._protocol_failure("plugin worker wrote output before the request")

    def _check_response_envelope(self, response: dict[str, Any], request_id: str, *,
                                 allowed: frozenset[str]) -> None:
        """応答の形を確かめる: 未知 field なし、`id` が要求と同じ、`pid` が Popen.pid、
        `ok` が bool、受信側に余分な bytes が無い。違反は protocol 違反 (kill + reap)。

        `id` は応答の出所の証明ではない (plugin は同じ process で id を観測できる)。
        要求と応答のずれと、先回りの応答を排除するためのもの。"""
        assert self._proc is not None
        if self._stdout_buffer:
            self._stdout_buffer.clear()
            self._protocol_failure("plugin worker wrote more than one line per request")
        if (any(key not in allowed for key in response)
                or response.get("id") != request_id
                or not _same_value(response.get("pid"), int(self._proc.pid))
                or not isinstance(response.get("ok"), bool)):
            self._protocol_failure("plugin worker response does not match the request")

    def _ensure_pipe_ownership(self) -> None:
        proc = self._proc
        assert proc is not None
        if self._stdin_fd is None:
            assert proc.stdin is not None
            fd = proc.stdin.fileno()
            # 書き込みを deadline で打ち切れるように (worker 側の読み口には影響しない)
            os.set_blocking(fd, False)
            self._stdin_fd = fd
        if self._stdout_fd is None:
            assert proc.stdout is not None
            self._stdout_fd = proc.stdout.fileno()

    def _close_parent_fds(self) -> None:
        """Close each parent stream once and detach it from the process object."""
        proc = self._proc
        if proc is None:
            return
        if self._stdin_fd is None and proc.stdin is not None:
            self._stdin_fd = proc.stdin.fileno()
        if self._stdout_fd is None and proc.stdout is not None:
            self._stdout_fd = proc.stdout.fileno()
        stdin = proc.stdin
        if self._stdin_fd is not None:
            self._stdin_fd = None
            proc.stdin = None
            if stdin is not None:
                try:
                    stdin.close()
                except OSError:
                    pass
        stdout = proc.stdout
        self._stdout_fd = None
        proc.stdout = None
        if stdout is not None:
            try:
                stdout.close()
            except OSError:
                pass

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        """1 回の plugin 呼び出し。payload は kind に応じて `df` (pandas
        DataFrame)・`params` (dict)・(strategy のみ) `indicators`/`signals`
        を含む dict。戻り値は kind 別に検証済みの dict:
        indicator は `{name: float}` そのもの、strategy は
        `StrategyDecision` 互換 dict、**signal だけは自然な形が
        `list[dict]` なため** brief の `call() -> dict` 契約に合わせて
        `{"signals": [検証済み dict, ...]}` にラップして返す。
        """
        self._check_owner_thread()
        with self._lock:
            return self._call_locked(payload)

    def _call_locked(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self._dead or self._proc is None:
            raise SandboxError(
                "plugin session is not usable (not started, or a previous "
                "timeout/crash/oversize-output killed it)")

        kind = self._meta.kind
        required = _KIND_PAYLOAD_KEYS[kind]
        missing = [k for k in required if k not in payload]
        if missing:
            raise SandboxError(f"call payload missing required key(s): {missing}")

        request: dict[str, Any] = {}
        for key in required:
            request[key] = _df_to_wire(payload[key]) if key == "df" else payload[key]
        request_id = secrets.token_hex(8)
        request["id"] = request_id
        self._check_no_stray_output()

        # 1 回の call は 1 つの deadline を書き込みと読み取りで共有する
        # (`sandbox_timeout_sec` が call 全体の上限)
        deadline = time.monotonic() + self._settings.sandbox_timeout_sec
        try:
            self._write_line(request, deadline=deadline)
        except _WriteDeadlineExpired:
            # load の後なので、読取の期限切れと同じ `timeout`
            raise self._deadline_expired(self._settings.sandbox_timeout_sec) from None
        except SandboxError:
            # `_write_line` 内の json.dumps 失敗 (payload に numpy スカラー
            # 等シリアライズ不能な値が混入) — 実際にはまだ何もパイプへ
            # 書き込んでいないため、セッションは継続利用可能 (レビュー
            # fix round 1 F5)。
            raise
        except OSError as exc:
            self._dead = True
            self.error_code = "protocol_error"
            # 終了状態が見えるまでの数 ms で kill すると、親 kill 扱いになって
            # 死因 (CPU 上限など) が失われる。
            self._await_reap_grace(deadline)
            recovered_without_kill = self._kill()
            if self.worker_unreaped:
                code = "crashed"
            elif recovered_without_kill:
                classified = self._worker_error("plugin worker exited unexpectedly (EOF)")
                raise SandboxError(f"failed to write to plugin worker: {exc}",
                                   code=classified.code,
                                   sandbox_reason=classified.sandbox_reason) from exc
            else:
                code = "protocol_error"
            raise SandboxError(f"failed to write to plugin worker: {exc}", code=code) from exc

        response = self._read_response(_remaining(deadline),
                                       self._settings.sandbox_output_max_bytes)
        # 応答の pid は Popen.pid と照合するだけで保持しない
        self._check_response_envelope(
            response, request_id, allowed=frozenset({"ok", "result", "error", "pid", "id"}))
        if not response["ok"]:
            self.error_code = "plugin_error"
            raise SandboxError(
                untrusted_text(response.get("error"), fallback="unknown plugin error"),
                code="plugin_error")
        if self._reap_worker():
            # 応答の後に死んでいた worker の結果は使わない
            self._dead = True
            self._kill()
            raise self._worker_error("plugin worker exited after its response")
        if "result" not in response:
            self._protocol_failure("plugin worker response has no result")

        result = response.get("result")
        # load 後の応答は候補が制御し得る入力。検証の失敗はどの例外でも
        # SandboxError にし、文言は worker 由来の文字列と同じ規則で整える
        try:
            if kind == "indicator":
                return _validate_indicator_result(result, outputs=self._meta.outputs,
                                                  df_index=payload["df"].index)
            if kind == "signal":
                return {"signals": _validate_signal_result(result)}
            return _validate_strategy_result(result)
        except SandboxError as exc:
            raise SandboxError(untrusted_text(str(exc), fallback="invalid plugin result"),
                               code=exc.code) from exc
        except Exception as exc:  # noqa: BLE001  OverflowError・RecursionError 等
            raise SandboxError(f"invalid plugin result ({type(exc).__name__})") from exc

    # -- IPC 詳細 --

    def _write_line(self, obj: dict[str, Any], *, deadline: float) -> None:
        """1 行書き込む。**例外の型で「シリアライズ失敗 (書き込み前、
        セッション継続可)」と「I/O 失敗 (壊れたパイプ、セッション死亡)」
        を呼び出し元が区別できるようにする** (レビュー fix round 1 F5):
        `json.dumps` が `TypeError`/`ValueError` (payload に numpy スカラー
        等シリアライズ不能な値が混入) を出したら `SandboxError` として
        送出し、実際のパイプ書き込みで失敗したら `OSError` をそのまま
        伝播させる (呼び出し元の `call()`/`__enter__` が使い分ける)。

        stdin は non-blocking にしてあり、書ける分だけ書いて POLLOUT を待つ。worker が
        stdin を読まずに pipe が埋まっても、`deadline` を過ぎれば
        `_WriteDeadlineExpired` で戻る (期限切れの分類は呼び出し元が決める)。
        """
        assert self._proc is not None
        self._ensure_pipe_ownership()
        assert self._stdin_fd is not None
        try:
            data = json.dumps(obj).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise SandboxError(f"failed to serialize request: {exc}") from exc
        fd = self._stdin_fd
        view = memoryview(data)
        poller: Any = None
        while view:
            try:
                sent = os.write(fd, view)
            except BlockingIOError:
                sent = 0
            except InterruptedError:
                continue
            if sent:
                view = view[sent:]
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _WriteDeadlineExpired()
            if poller is None:
                poller = select.poll()
                poller.register(fd, select.POLLOUT)
            try:
                # POLLERR・POLLHUP でも戻り、次の write が BrokenPipeError になる
                poller.poll(max(1, min(20, math.ceil(remaining * 1000))))
            except InterruptedError:
                pass

    def _read_response(self, timeout_sec: float, max_bytes: int) -> dict[str, Any]:
        """1 行を「timeout・出力上限超過・EOF」いずれかに達するまで読む。
        **出力を無制限にバッファしない**
        (`max_bytes` を超えた時点で読み取りを打ち切る)。timeout/oversize/
        EOF はすべてセッションを使用不能にする。`max_bytes` は呼び出し元
        が渡す — 起動応答 (ready) は worker 自身が生成する小さな固定文言
        なので `_STARTUP_MAX_BYTES` を使い、call() の応答は
        `settings.sandbox_output_max_bytes` (plugin コードの出力なので
        こちらは利用者設定に従う) を使う、と使い分けるため。
        """
        assert self._proc is not None
        self._ensure_pipe_ownership()
        assert self._stdout_fd is not None
        deadline = time.monotonic() + timeout_sec
        poller = select.poll()
        poller.register(self._stdout_fd, select.POLLIN)
        worker_gone = False
        while True:
            newline = self._stdout_buffer.find(b"\n")
            if newline != -1:
                if newline + 1 > max_bytes:
                    return self._protocol_failure(
                        f"plugin worker output exceeded {max_bytes} bytes")
                payload = bytes(self._stdout_buffer[:newline])
                del self._stdout_buffer[:newline + 1]
                try:
                    decoded = parse_worker_line(payload)
                except _ProtocolViolation as exc:
                    self._stdout_buffer.clear()
                    self._protocol_failure(
                        f"plugin worker returned invalid JSON: {exc}")
                    raise AssertionError("unreachable") from exc
                if not isinstance(decoded, dict):
                    return self._protocol_failure(
                        "plugin worker returned a JSON value that is not an object")
                if worker_gone:
                    # 起動中 (load 前) の最後の行だけは隔離段の固定応答として読む。
                    # stderr はここで閉じない: 診断ログは分類 (code・sandbox_reason) が
                    # 決まった後の close() で 1 回だけ出す
                    self._dead = True
                    self._close_parent_fds()
                return decoded
            if len(self._stdout_buffer) >= max_bytes:
                return self._protocol_failure(
                    f"plugin worker output exceeded {max_bytes} bytes")

            if self._reap_worker():
                # load 前の隔離段の固定応答と、load への正規の 1 応答 (plugin_ready) は
                # 死んだ worker が残していても読む。それ以外 (call 応答) は採用しない
                # (plugin が protocol fd へ直接書き得る、§2.5)
                if (self._awaiting_load or self._reading_load_response) \
                        and self._drain_dead_worker_stdout(max_bytes):
                    worker_gone = True
                    continue
                self._dead = True
                raise self._worker_error("plugin worker exited unexpectedly (EOF)")

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if self._reap_worker():
                    if (self._awaiting_load or self._reading_load_response) \
                            and self._drain_dead_worker_stdout(max_bytes):
                        worker_gone = True
                        continue
                    self._dead = True
                    raise self._worker_error("plugin worker exited unexpectedly (EOF)")
                raise self._deadline_expired(timeout_sec)

            try:
                events = poller.poll(max(0, min(20, int(remaining * 1000))))
            except (InterruptedError, BlockingIOError):
                events = []
            except OSError as exc:
                return self._protocol_failure(
                    f"error reading plugin worker output: {exc}")
            if events:
                event_mask = 0
                for _fd, mask in events:
                    event_mask |= mask
                if event_mask & (select.POLLIN | select.POLLHUP | select.POLLERR):
                    try:
                        chunk = os.read(self._stdout_fd, 65536)
                    except (InterruptedError, BlockingIOError):
                        continue
                    except OSError as exc:
                        return self._protocol_failure(
                            f"error reading plugin worker output: {exc}")
                    if not chunk:
                        self._dead = True
                        self._await_reap_grace(deadline)
                        self._kill()
                        raise self._worker_error(
                            "plugin worker exited unexpectedly (EOF)")
                    self._stdout_buffer.extend(chunk)

    def _deadline_expired(self, timeout_sec: float) -> SandboxError:
        """読み書きの期限切れ。worker を kill + reap して分類した例外を返す。

        load を送り切る前は `sandbox_unavailable / sandbox_startup_timeout`、後は
        `timeout`。kill の前に終わっていた worker は、その終了状態で分類する。"""
        self._dead = True
        self.error_code = "timeout"
        recovered_without_kill = self._kill()
        if self.worker_unreaped:
            return SandboxError(f"plugin call timed out after {timeout_sec}s", code="crashed")
        if recovered_without_kill:
            return self._worker_error("plugin worker exited unexpectedly (EOF)")
        if self._awaiting_load:
            return self._unavailable("sandbox_startup_timeout")
        return SandboxError(f"plugin call timed out after {timeout_sec}s", code="timeout")

    def _await_reap_grace(self, deadline: float) -> bool:
        """終了状態が wait4 に見えるまで、`deadline` を超えない範囲で
        `_EOF_REAP_GRACE_SEC` だけ待つ。回収できたら True。"""
        grace_end = min(deadline, time.monotonic() + _EOF_REAP_GRACE_SEC)
        while True:
            if self._reap_worker():
                return True
            if time.monotonic() >= grace_end:
                return False
            time.sleep(_EOF_REAP_POLL_SEC)

    def _drain_dead_worker_stdout(self, max_bytes: int) -> bool:
        """死亡を観測した worker が残したパイプ内容を、待たずに buffer へ取り込む。

        孫が stdout を握っていても EOF を待たないよう、読める間だけ読み、
        EOF か「今は読めない」で止める。改行が揃うか上限に達したら True
        (呼び出し側が通常の行判定・上限判定へ戻る)。"""
        fd = self._stdout_fd
        if fd is None:
            return False
        poller = select.poll()
        poller.register(fd, select.POLLIN)
        while (b"\n" not in self._stdout_buffer
               and len(self._stdout_buffer) < max_bytes):
            try:
                if not poller.poll(0):
                    break
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            self._stdout_buffer.extend(chunk)
        return (b"\n" in self._stdout_buffer
                or len(self._stdout_buffer) >= max_bytes)

    def _protocol_failure(self, message: str) -> dict[str, Any]:
        self._dead = True
        self.error_code = "protocol_error"
        recovered_without_kill = self._kill()
        if self.worker_unreaped:
            raise SandboxError(message, code="crashed")
        if recovered_without_kill:
            raise self._worker_error("plugin worker exited unexpectedly (EOF)")
        if self._awaiting_load:
            # 隔離の報告より前の壊れた応答。plugin はまだ読まれていない
            raise self._unavailable("attestation_unexpected_message")
        raise SandboxError(message, code="protocol_error")

    def _kill(self) -> bool:
        """Kill the process group and report a reap completed without parent kill.

        killpg が ESRCH 以外 (EPERM 等) で失敗した場合も、kill を送れずに回収
        できたなら親 kill による死ではないので同じ扱いにする。"""
        if self._proc is None:
            return False
        if self._reap_worker():
            return True
        self.parent_kill_sent = True
        kill_not_sent = False
        # The legacy property intentionally keeps its old timeout contract.
        self._cpu_sec = None
        try:
            # 直前の wait4 が「まだ回収していない自分の子」を返した区間で送る
            self._send_group_kill()
        except ProcessLookupError:
            self.parent_kill_sent = False
            kill_not_sent = True
        except OSError:
            self.parent_kill_sent = False
            kill_not_sent = True
        deadline = time.monotonic() + _KILL_REAP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if self._reap_worker():
                return kill_not_sent
            time.sleep(0.01)
        self.worker_unreaped = True
        self.worker_cpu_sec = None
        self.error_code = "crashed"
        self._close_parent_fds()
        self._close_stderr()
        with _ORPHANS_LOCK:
            if self not in _ORPHANS:
                global _ORPHAN_OVERFLOW_LOGGED
                _ORPHANS.append(self)
                if len(_ORPHANS) > _ORPHAN_CAPACITY and not _ORPHAN_OVERFLOW_LOGGED:
                    _ORPHAN_OVERFLOW_LOGGED = True
                    _log.warning("plugin worker orphan list exceeds %d entries",
                                 _ORPHAN_CAPACITY)
        return False

    def _reap_worker(self) -> bool:
        """Record the one authoritative wait status before any classification."""
        proc = self._proc
        if proc is None or self.worker_returncode is not None or self._collected_elsewhere:
            return self.worker_returncode is not None or self._collected_elsewhere
        try:
            pid, status, usage = os.wait4(proc.pid, os.WNOHANG)
        except ChildProcessError:
            # SIGCHLD=SIG_IGN や他の waiter が先に回収済み。終了状態は分から
            # ないが、待ち続けても永久に回収できないので終端として扱う。
            self._collected_elsewhere = True
            self._kill_leftover_group()
            return True
        if pid == 0:
            return False
        rc = os.waitstatus_to_exitcode(status)
        proc.returncode = rc
        self.worker_returncode = rc
        self.worker_signal = -rc if rc < 0 else None
        self.worker_cpu_sec = float(usage.ru_utime + usage.ru_stime)
        if not self.parent_kill_sent:
            self._cpu_sec = self.worker_cpu_sec
        return True

    def _open_child_pidfd(self, pid: int) -> int | None:
        """Popen 直後の worker の pidfd を取る。取れない環境 (syscall が無い等) と、
        取った fd が自分の未回収の子を指していない場合 (Popen から今までの間に
        他所で回収され、番号が再利用された) は None。"""
        try:
            fd = _pidfd_open(pid)
        except OSError as exc:
            _log.info("plugin worker pidfd unavailable plugin=%s (%s); signals use the "
                      "pid only while the worker is known to be unreaped",
                      self._meta.name, errno.errorcode.get(exc.errno or 0, "unknown"))
            return None
        try:
            os.waitid(_P_PIDFD, fd, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except OSError:
            os.close(fd)
            return None
        return fd

    def _send_group_kill(self) -> None:
        """worker の process group へ SIGKILL を送る。失敗は OSError のまま返す。

        start_new_session=True で worker の pid == pgid。数値の killpg は、呼び出し元が
        直前の wait4 で「まだ回収していない自分の子」と確かめた区間でだけ使う
        (回収前の pid と、それを pgid とする group は再利用されない)。"""
        assert self._proc is not None
        with self._pidfd_lock:
            pidfd = self._pidfd
            if pidfd is not None and not self._group_signal_unsupported:
                try:
                    _pidfd_send_signal(pidfd, signal.SIGKILL, _PIDFD_SIGNAL_PROCESS_GROUP)
                    return
                except OSError as exc:
                    if exc.errno != errno.EINVAL:
                        raise
                    # group 指定の無い kernel (6.9 未満)。数値の killpg に戻る
                    self._group_signal_unsupported = True
        os.killpg(self._proc.pid, signal.SIGKILL)

    def _kill_leftover_group(self) -> None:
        """リーダーが他所で回収された後も、同じ group の孫が残りうる。死因は
        不明なので parent_kill_sent は立てず、best-effort で 1 回だけ送る。

        回収済みの pid / pgid の番号は別 process に再利用され得るので、数値では
        送らない。pidfd が無い・group 指定が使えない環境では孫を諦める。"""
        if self._leftover_kill_sent:
            return
        self._leftover_kill_sent = True
        with self._pidfd_lock:
            pidfd = self._pidfd
            if pidfd is None or self._group_signal_unsupported:
                _log.info("plugin worker collected elsewhere plugin=%s; leftover group "
                          "not signalled (no pidfd group signal)", self._meta.name)
                return
            try:
                _pidfd_send_signal(pidfd, signal.SIGKILL, _PIDFD_SIGNAL_PROCESS_GROUP)
            except OSError:
                pass

    def _interrupt_from_other_thread(self) -> None:
        """owner 以外の thread の close が、owner の読み書きの終わりを待たずに済む
        よう、worker の group を pidfd で kill する。pidfd が無ければ何もしない
        (数値の pid は owner の回収と競合し得るので、ここでは使わない)。"""
        with self._pidfd_lock:
            pidfd = self._pidfd
            if (pidfd is None or self.worker_returncode is not None
                    or self._collected_elsewhere):
                return
            # owner の分類が「親 kill」になるよう、送る前に立てる
            previous = self.parent_kill_sent
            self.parent_kill_sent = True
            flags = 0 if self._group_signal_unsupported else _PIDFD_SIGNAL_PROCESS_GROUP
            try:
                _pidfd_send_signal(pidfd, signal.SIGKILL, flags)
            except OSError as exc:
                if flags and exc.errno == errno.EINVAL:
                    self._group_signal_unsupported = True
                    try:
                        _pidfd_send_signal(pidfd, signal.SIGKILL, 0)
                        return
                    except OSError:
                        pass
                self.parent_kill_sent = previous

    def _close_pidfd(self) -> None:
        with self._pidfd_lock:
            fd = self._pidfd
            self._pidfd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _worker_error(self, message: str) -> SandboxError:
        """worker の終了 (親が見た wait4 の結果) から分類する。

        load 前の終了は plugin が動き得なかった失敗なので `sandbox_unavailable`
        (SIGSYS は `startup_sigsys_unattributed`、それ以外は CPU 上限死も含めて
        `worker_bootstrap_failed`)。load 後に親 kill なしで SIGSYS なら、禁止 syscall と
        同 uid の外部 signal を区別できないので原因未確定の `crashed`。kernel log の
        有無では分類を変えない。"""
        sigsys = (not self.parent_kill_sent and self.worker_signal == signal.SIGSYS)
        if self.worker_unreaped:
            self.error_code = "crashed"
            return SandboxError(message, code="crashed")
        if self._awaiting_load:
            if sigsys:
                self._report_sigsys()
                return self._unavailable("startup_sigsys_unattributed")
            return self._unavailable("worker_bootstrap_failed")
        if sigsys:
            self._report_sigsys()
            self.error_code = "crashed"
            self.sandbox_reason = "sigsys_unattributed"
            return SandboxError(SIGSYS_CRASH_MESSAGE, code="crashed",
                                sandbox_reason="sigsys_unattributed")
        if (self._closing and not self.parent_kill_sent
                and self.worker_returncode == 0):
            # close op に応じて正常終了した worker。応答行は死後なので読まないが、
            # 終了状態は正常なので session の分類を crashed に汚染しない
            # (close() はこの例外を握りつぶす)
            return SandboxError(message, code="crashed")
        code = "crashed"
        if (not self.parent_kill_sent and self.worker_signal == signal.SIGKILL
                and self.worker_cpu_sec is not None
                and self.worker_cpu_sec >= self._settings.sandbox_session_cpu_sec - _CPU_TOLERANCE_SEC):
            code = "cpu_limit"
        self.error_code = code
        return SandboxError(message, code=code)

    def _report_sigsys(self) -> None:
        """SIGSYS で終わった worker について、運用者向けの固定 1 行を 1 回だけ出す。"""
        if self._sigsys_reported or self._proc is None:
            return
        self._sigsys_reported = True
        _log.warning("%s; cause is a forbidden syscall or an external signal "
                     "(not attributed to the plugin)",
                     seccomp.sigsys_diagnostic_line(self._proc.pid))

    def _close_stderr(self) -> None:
        f = self._stderr_file
        self._stderr_file = None
        if f is None:
            if self.stderr_unavailable and not self._stderr_unavailable_logged:
                self._stderr_unavailable_logged = True
                _log.info("plugin_worker_diagnostic plugin=%s code=%s cpu_sec=%s returncode=%s signal=%s stderr_unavailable=true%s",
                          self._meta.name, self.error_code, self.worker_cpu_sec,
                          self.worker_returncode, self.worker_signal,
                          (f" sandbox_reason={self.sandbox_reason}"
                           if self.sandbox_reason is not None else ""))
            return
        try:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8192))
            raw = f.read(8192)
            text = raw.decode("utf-8", "backslashreplace")
            # 診断で最も価値があるのは末尾 (最後の例外行) なので、escape で
            # 上限を超える分は先頭側から落とす。escape 単位は壊さない。
            escaped_parts: list[str] = []
            escaped_size = 0
            escaped_truncated = False
            for ch in reversed(text):
                unit = (ch if ch != "\\" and unicodedata.category(ch) not in {"Cc", "Cf", "Zl", "Zp"}
                        else ch.encode("unicode_escape").decode("ascii"))
                if escaped_size + len(unit.encode("utf-8")) > 8192:
                    escaped_truncated = True
                    break
                escaped_parts.append(unit)
                escaped_size += len(unit.encode("utf-8"))
            escaped = "".join(reversed(escaped_parts))
            self.stderr_tail = escaped or None
            fields = f"plugin={self._meta.name} code={self.error_code} cpu_sec={self.worker_cpu_sec} returncode={self.worker_returncode} signal={self.worker_signal} stderr_unavailable={self.stderr_unavailable}"
            if self.sandbox_reason is not None:
                fields += f" sandbox_reason={self.sandbox_reason}"
            if size > 8192 or escaped_truncated:
                fields += " truncated=true"
            if escaped:
                fields += f" stderr_tail={escaped}"
            _log.info("plugin_worker_diagnostic %s", fields)
        except Exception as exc:
            # 診断は補助。読めなくても close 系の契約 (例外を出さない) を守る。
            _log.info("plugin_worker_diagnostic plugin=%s stderr_read_failed=%r",
                      self._meta.name, exc)
        finally:
            try:
                f.close()
            except Exception:
                pass


def run_plugin(meta: PluginMeta, payload: dict[str, Any], *,
               timeout_sec: float | None = None,
               settings: "PluginSettings",
               resolved: "ResolvedIndicatorSet | None" = None) -> dict[str, Any]:
    """「セッション 1 回だけ」の薄いラッパ (producer 等の単発評価用)。
    バックテストのように同一 plugin を大量に評価する場合は `PluginSession`
    を直接使い、プロセス起動コストを 1 回に償却すること。`resolved` は
    `PluginSession` にそのまま転送する (strategy kind で必須)。
    """
    eff_settings = settings
    if timeout_sec is not None:
        # `PluginSettings.sandbox_timeout_sec` は pydantic の `Field(gt=0)`
        # で検証されるが、`model_copy(update=...)` はバリデータを一切
        # 再実行しない (pydantic の既知の挙動) — ここで検証せず素通しする
        # と、0/負値が待機 API まで届いてから生の
        # `ValueError` になり「SandboxError 1 種だけ catch すればよい」
        # 契約を破る (レビュー fix round 1 F5)。ここで明示的に fail closed。
        if (isinstance(timeout_sec, bool) or not isinstance(timeout_sec, (int, float))
                or not math.isfinite(timeout_sec) or timeout_sec <= 0):
            raise SandboxError(
                f"timeout_sec must be a finite positive number, got {timeout_sec!r}")
        eff_settings = settings.model_copy(update={"sandbox_timeout_sec": timeout_sec})
    with PluginSession(meta, settings=eff_settings, resolved=resolved) as session:
        return session.call(payload)


# --- DataFrame wire 変換 (親側: DataFrame → wire) -----------------------

_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def _df_to_wire(df: "pd.DataFrame") -> dict[str, Any]:
    """DataFrame → JSON 安全な wire 表現。worker.py の `_wire_to_df` と対
    (モジュール docstring のワイヤ形式を参照)。"""
    import pandas as pd

    index = df.index
    if not isinstance(index, pd.DatetimeIndex):
        raise SandboxError("df index must be a DatetimeIndex")
    if index.tz is None:
        raise SandboxError("df index must be tz-aware UTC")
    wire: dict[str, Any] = {
        "index": [ts.isoformat() for ts in index.tz_convert("UTC")]}
    for col in _OHLCV_COLUMNS:
        if col not in df.columns:
            raise SandboxError(f"df missing required column: {col!r}")
        wire[col] = df[col].tolist()
    return wire


# --- 戻り値のスキーマ検証 (kind 別) -------------------------------------

def _finite_float(value: Any) -> bool:
    """float に変換でき、有限であるか。巨大な整数 (10**400) の OverflowError も偽。"""
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError, TypeError):
        return False


def _validate_indicator_result(result: Any, *,
                               outputs: "tuple[str, ...] | None",
                               df_index: Any = None) -> dict[str, Any]:
    """standalone (`run_plugin` / `PluginSession.call(kind="indicator")`) の
    応答を親側で**再検証**する ([indicator-consumption-wiring] §2.5)。

    worker は既に `plugin_contract.validate_indicator_result` を通した
    値を wire 形式 (`{key: float | {"series": [float|null, ...]}}`) で返して
    いるが、信頼境界を跨いだ値なのでここで形だけもう一度見る。
    NaN は wire 上で `null` になっている。戻り値は
    `{key: float | list[float | None]}`。

    **codex plan r2 束1 Important**: wire 形式を Python 値
    (`float | list[float|None]`) へ復元した**後**、
    `core.plugin_contract.validate_indicator_result(out, df_index=None,
    outputs=outputs)` を必ず通す。`df_index=None` なので系列長は検査しない
    (wire 変換の時点で `list` になっており、系列長は worker 側で既に
    `df` に対して検査済み — ここで二重にやり直せるのは `outputs` 宣言との
    キー集合一致のみ、これが親側で唯一欠けていた検査)。`outputs=None`
    (standalone 宣言なし indicator、S1) は任意のキー集合を許す (従来どおり)。
    """
    if not isinstance(result, dict):
        raise SandboxError(
            f"indicator must return a dict, got {type(result).__name__}")
    out: dict[str, Any] = {}
    for key, value in result.items():
        if not isinstance(key, str):
            raise SandboxError(f"indicator result keys must be str, got {key!r}")
        if isinstance(value, dict):
            series = value.get("series")
            if set(value) != {"series"} or not isinstance(series, list):
                raise SandboxError(
                    f"indicator result[{key!r}] series envelope is malformed")
            for item in series:
                if item is None:
                    continue
                if isinstance(item, bool) or not isinstance(item, (int, float)):
                    raise SandboxError(
                        f"indicator result[{key!r}] series must contain "
                        f"numbers or null, got {item!r}")
                if not _finite_float(item):
                    raise SandboxError(
                        f"indicator result[{key!r}] series must be finite")
            out[key] = [None if item is None else float(item) for item in series]
            continue
        if value is None:
            out[key] = None       # スカラー NaN (wire では null)
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SandboxError(
                f"indicator result[{key!r}] must be a number, got {value!r}")
        if not _finite_float(value):
            raise SandboxError(
                f"indicator result[{key!r}] must be finite")
        out[key] = float(value)

    from agentic_fx.core.plugin_contract import (
        IndicatorResultError, validate_indicator_result as _validate_common)
    # codex r1 束1 Important: 共通 validator は **plugin の生の戻り値**を
    # 見る前提なので、スカラーの `None` は無条件に拒否する (`_check_number`)。
    # 一方 wire では scalar NaN が `null` になり、上のループで `None` へ
    # 復元されている — そのまま渡すと `run_plugin` 経由の scalar NaN が
    # 必ず `SandboxError` になり、S1 の「値が未確定のキーを落とす」へ
    # 到達できない。検証の間だけ NaN へ戻し、検証後に契約形 (`None`) を
    # 復元する (系列の要素 `None` は共通 validator が NaN として受理する
    # ので変換不要)。
    nan_keys = [key for key, value in out.items() if value is None]
    for key in nan_keys:
        out[key] = float("nan")
    try:
        # 系列の長さは親が渡した df の index でも確かめる (worker の検証に頼らない)
        _validate_common(out, df_index=df_index, outputs=outputs)
    except IndicatorResultError as exc:
        raise SandboxError(str(exc)) from exc
    except (OverflowError, ValueError, TypeError) as exc:
        raise SandboxError(f"indicator result is not representable ({type(exc).__name__})") from exc
    finally:
        for key in nan_keys:
            out[key] = None
    return out


_SIGNAL_ALLOWED_KEYS = frozenset(
    {"direction", "strength", "rationale", "stop_loss", "take_profit"})

# rationale の最大長 (最終レビュー F6, codex Important — 注入面縮小)。
# rationale は plugin (LLM/人間問わず) が自由記述する説明文で、そのまま
# 取引判断 loop のプロンプトに展開される — non-empty str 検証だけでは
# 無制限長を許してしまい、プロンプトインジェクションの土台やコンテキスト
# 肥大化に使われ得る。境界は「2000 文字は通す、2001 文字は reject」
# (`len() > _RATIONALE_MAX_LEN` — ちょうど上限は許容)。
_RATIONALE_MAX_LEN = 2000


def _validate_signal_result(result: Any) -> list[dict[str, Any]]:
    if not isinstance(result, list):
        raise SandboxError(f"signal must return a list, got {type(result).__name__}")
    out: list[dict[str, Any]] = []
    for i, item in enumerate(result):
        if not isinstance(item, dict):
            raise SandboxError(f"signal[{i}] must be a dict")
        # bar_ts はハーネス (producer/signal_eval, プラン 7 Task 7/8) が
        # 評価対象バケットから設定する監査キー。plugin の任意値を受理する
        # と鮮度ゲート回避・誤 abandoned・dedupe 不成立が起きる
        # (codex R3 I4) — 明示的に別メッセージで拒否する。
        if "bar_ts" in item:
            raise SandboxError(
                f"signal[{i}] must not include 'bar_ts' "
                "(harness-owned audit key, not plugin-settable)")
        unknown = set(item) - _SIGNAL_ALLOWED_KEYS
        if unknown:
            raise SandboxError(f"signal[{i}] has unknown keys: {sorted(unknown)}")

        try:
            direction = Direction(item.get("direction"))
        except ValueError as exc:
            raise SandboxError(
                f"signal[{i}].direction invalid: {item.get('direction')!r}") from exc

        strength = item.get("strength")
        if isinstance(strength, bool) or not isinstance(strength, (int, float)):
            raise SandboxError(f"signal[{i}].strength must be a number")
        if not _finite_float(strength) or not (0.0 <= float(strength) <= 1.0):
            raise SandboxError(f"signal[{i}].strength must be in [0, 1]")
        strength = float(strength)

        # fix round 2 F8 残ギャップ: strategy 経路は StrategyDecision を
        # 実構築するため contracts._require_nonempty_str (非空 str 検証)
        # が自動的にかかるが、signal 経路は Signal オブジェクトを構築
        # せず手組み検証のため、ここだけ空文字列 "" が素通りしていた。
        # contracts._require_nonempty_str と同じ規則をここでも適用する
        # (import して共有はしない — private ヘルパーのクロスモジュール
        # import は本コードベースの既存慣習に反する。sandbox.py は
        # Signal の全フィールドを持たない — bar_ts/pair/timeframe/plugin
        # はハーネスが後付けするため、ここで実際に Signal を構築する
        # ことはできない — ので、意味論だけを手組みで再現する)。
        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale:
            raise SandboxError(f"signal[{i}].rationale must be a non-empty str")
        if len(rationale) > _RATIONALE_MAX_LEN:
            raise SandboxError(
                f"signal[{i}].rationale exceeds max length "
                f"{_RATIONALE_MAX_LEN} (got {len(rationale)})")

        validated: dict[str, Any] = {
            "direction": direction.value, "strength": strength, "rationale": rationale}
        for opt in ("stop_loss", "take_profit"):
            if opt not in item:
                continue
            value = item[opt]
            if value is None:
                validated[opt] = None
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not _finite_float(value) or value <= 0):
                raise SandboxError(
                    f"signal[{i}].{opt} must be a finite positive number")
            validated[opt] = float(value)
        out.append(validated)
    return out


_STRATEGY_ALLOWED_KEYS = frozenset(
    {"action", "rationale", "direction", "entry_type", "limit_price",
     "stop_loss", "take_profit"})


def _validate_strategy_result(result: Any) -> dict[str, Any]:
    """`contracts.StrategyDecision` を実際に構築してみて `ValueError`/
    `TypeError` (必須フィールド欠落) を `SandboxError` へ写像する — brief
    と同じ検証ロジックを二重実装しないための正道 (action="exit" 等の
    語彙外の値、open で stop_loss/direction/entry_type 欠落・非正値は
    すべて `StrategyDecision.__post_init__` が拾う)。

    **rationale の最大長だけはここで独自に検査する** (F6): `contracts.
    _require_nonempty_str` は非空 str のみを見て長さは見ない
    (`contracts.py` は plugin 専用モジュールではなく無制限長を許す既存
    呼び出し元があるため、上限をそちらへ足さない)。非 str の場合は長さを
    測らず `StrategyDecision.__post_init__` 側の型検証に委ねる。
    """
    if not isinstance(result, dict):
        raise SandboxError(f"strategy must return a dict, got {type(result).__name__}")
    unknown = set(result) - _STRATEGY_ALLOWED_KEYS
    if unknown:
        raise SandboxError(f"strategy result has unknown keys: {sorted(unknown)}")

    rationale_raw = result.get("rationale")
    if isinstance(rationale_raw, str) and len(rationale_raw) > _RATIONALE_MAX_LEN:
        raise SandboxError(
            f"strategy result rationale exceeds max length "
            f"{_RATIONALE_MAX_LEN} (got {len(rationale_raw)})")

    kwargs = dict(result)
    action_raw = kwargs.pop("action", None)
    try:
        action = StrategyAction(action_raw)
        if kwargs.get("direction") is not None:
            kwargs["direction"] = Direction(kwargs["direction"])
        if kwargs.get("entry_type") is not None:
            kwargs["entry_type"] = EntryType(kwargs["entry_type"])
        decision = StrategyDecision(action=action, **kwargs)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SandboxError(f"invalid strategy result: {exc}") from exc

    return {
        "action": decision.action.value,
        "rationale": decision.rationale,
        "direction": decision.direction.value if decision.direction else None,
        "entry_type": decision.entry_type.value if decision.entry_type else None,
        "limit_price": decision.limit_price,
        "stop_loss": decision.stop_loss,
        "take_profit": decision.take_profit,
    }
