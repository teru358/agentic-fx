"""plugin コードをサンドボックス実行する (プラン 7 Task 2、設計書 §6)。

**脅威モデル (必読)**: `check_source` は AST 上の**名前出現**に基づく静的
検査であり、動的呼び出し (`getattr(obj, "e" + "val")` のような文字列組み
立て、オブジェクト内省による sandbox escape 等) を防ぐものではない。
`worker.py` の resource limit・import 遮断も同様に「善意だが不注意な
plugin コードの事故 (無限ループ・OOM・意図しない I/O)」を防ぐことが目的
であり、悪意ある攻撃者からの完全な隔離を保証しない。**最終防衛線は人間
承認** (§6 — plugin は承認されるまで discover/実行されず、承認は
content_hash 一致を人間がレビューした版に限定する)。

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

- handshake (セッション開始直後に親から送る 1 行のみ):
  `{"cpu_sec": int, "memory_mb": int, "nofile": int, "fsize_mb": int, "kind": "indicator"|"signal"|"strategy"}`
- ready (handshake に対する worker からの応答。plugin.py の import が
  成功したら送られる。**この 1 行だけは call() の応答ではなく起動応答**
  であり、`sandbox_timeout_sec` ではなく別枠の起動タイムアウトで待つ):
  `{"ok": true, "ready": true, "pid": int}` /
  `{"ok": false, "ready": false, "error": "<message>"}`
- request (call() のたびに親が送る 1 行):
  - kind=indicator/signal: `{"df": <df wire>, "params": {...}}`
  - kind=strategy: `{"df": <df wire>, "indicators": {...}, "signals": [...], "params": {...}}`
  - df wire (DataFrame の JSON 安全な表現。**index は UTC の ISO8601
    文字列**、カラムは OHLCV の 5 本固定):
    `{"index": [iso8601 str, ...], "open": [...], "high": [...],
      "low": [...], "close": [...], "volume": [...]}`
    float は Python の json エンコード/デコードが shortest-roundtrip
    表現 (repr アルゴリズム) であるため、往復させても bit-exact に一致
    する — 精度劣化を心配してカスタムシリアライザを書く必要はない。
- response (request 1 件につき 1 行):
  `{"ok": true, "result": <kind別 dict/list>, "pid": int}` または
  `{"ok": false, "error": "<message>", "pid": int}`
  — `result` の構造は worker から見て「plugin が返した生の値」であり、
  kind 別のスキーマ検証・harness 専有キー (`bar_ts`) の拒否は **すべて
  この親プロセス側 (sandbox.py) で行う** (worker は信頼境界の内側に
  近い実行体であり、検証ロジック — 特に `contracts.StrategyDecision` の
  import — を二重に持ち込みたくないため)。

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
import json
import math
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentic_fx.core.contracts import (
    Direction, EntryType, StrategyAction, StrategyDecision,
)
from agentic_fx.plugin.loader import PluginMeta, content_hash

if TYPE_CHECKING:
    import pandas as pd

    from agentic_fx.config import PluginSettings
    from agentic_fx.plugin.resolve import ResolvedIndicatorSet


class SandboxError(Exception):
    """plugin コード実行 (AST 検査・起動・呼び出し・戻り値検証) に起因
    する全エラーの単一表現。呼び出し側はこれ 1 種類だけを catch すれば
    よい (実装内訳を漏らさない)。"""


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
        self._proc: subprocess.Popen | None = None
        self._dead = False
        self._cpu_sec: float | None = None
        self.pid: int | None = None
        # プラン 8 B 束: PluginSession は単一スレッド所有が前提
        # (全使用箇所が単一スレッド — ロックは追加しない)。construction
        # したスレッドを記録し、実行時 assert で境界越えを検出する。
        self._owner_thread = threading.get_ident()

    @property
    def cpu_sec(self) -> float | None:
        """worker の累積 CPU 秒。**`close()` 完了後に確定する** —
        **正常終了・plugin error 後はいずれも float** (plugin コード自身の例外は
        セッションを `_dead` にしない既存契約なので graceful close が成立する)。
        **`None` は 2 経路のみ**: SIGKILL fallback (timeout 後の強制終了) と、
        `__enter__` 失敗による worker 未起動 (設計書 v1.4 §6 C1、ユーザー裁定 ④)。
        """
        return self._cpu_sec

    def _check_owner_thread(self) -> None:
        current = threading.get_ident()
        if current != self._owner_thread:
            raise RuntimeError(
                f"PluginSession used from a different thread than its "
                f"owner thread (owner={self._owner_thread}, "
                f"current={current}) — PluginSession is single-thread-owned")

    def __enter__(self) -> "PluginSession":
        """起動シーケンス全体を 1 つの try/except で包む (レビュー fix
        round 1 F2)。**`__enter__` が例外を投げると Python は `__exit__`
        を一切呼ばない** — 以前は個別の失敗パスでだけ `close()`/`_kill()`
        を呼んでいたため、想定していなかった失敗経路 (`subprocess.Popen`
        自体の例外等) では `self._proc` の stdin/stdout パイプが GC 任せ
        になり fd がリークし得た。ここで一括して `self.close()` を必ず
        通してから re-raise する (`SandboxError` はメッセージを保持して
        そのまま、それ以外の例外は `SandboxError` へ写像 — 呼び出し側が
        `SandboxError` 1 種だけ catch すればよい契約を `__enter__` でも
        維持する)。

        **実行時ハッシュ再検証 (プラン 7 Task 3 レビュー fix round 1 F1
        — TOCTOU 封鎖)**: `check_source`/`subprocess.Popen` より前に
        `loader.content_hash(self._meta.path)` を再計算し、`self._meta.
        content_hash` (呼び出し元がこの meta を取得した時点でのハッシュ
        — 通常は `plugin_loader.approved_plugins()` が起動時に承認と
        照合したもの) と不一致なら `SandboxError` を送出する。
        `approved_plugins()` は起動時 (discover 時点) にディスク内容を
        検証するだけで、実際に worker が plugin.py を import するのは
        その後の `get_indicators` 呼び出し時 — その間隔で plugin.py が
        承認内容と異なる内容に差し替えられても、再検証が無ければ古い
        承認のまま新しい (未承認の) コードが実行されてしまう
        (「承認は内容ハッシュに対して行う」設計書 §6 の実行時破れ)。
        ここでの再検証により、`run_plugin`/`PluginSession` を経由する
        全消費者 (本 task の `get_indicators` 合成、将来の signal/strategy
        評価) が一括で守られる。**なお、この再検証自体と実際の import
        (worker プロセス起動後) の間にも理論上ミリ秒級のレースが残る
        (再検証直後に書き換えられれば検出できない) — 本モジュールの
        脅威モデル (悪意ある攻撃者からの完全な隔離は保証せず、善意だが
        不注意な plugin コードの事故を防ぐことが目的) の範囲では許容する。
        """
        self._check_owner_thread()
        try:
            current_hash = content_hash(self._meta.path)
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
            # codex plan r2 束1 Minor: 契約は片方向だけでは不十分 —
            # strategy は `resolved` 必須 (上記) だが、indicator/signal は
            # `resolved` を**受け取ってはいけない** (`None` 固定)。ここを
            # 検査しないと、indicator/signal に誤って `ResolvedIndicatorSet`
            # を渡す呼び出しが静かに通り、依存情報 (indicator の実体パス等)
            # が handshake に混入し得る (kind 別の契約が片方向にしか pin
            # されていなかった)。
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
                    current = content_hash(real.parent)
                    if current != item.content_hash:
                        raise SandboxError(
                            f"indicator {item.plugin_name!r}: content changed "
                            "since resolution (hash mismatch) — refusing to "
                            f"execute (expected {item.content_hash}, got {current})")
                    check_source(real)
                indicator_specs = self._resolved.handshake_items()
                # 検査済みの実体パスだけを handshake に載せる
                for spec, item in zip(indicator_specs, self._resolved.items):
                    spec["plugin_py"] = str(Path(item.plugin_py).resolve())

            env = _build_env()
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "agentic_fx.plugin.worker",
                 str(self._meta.path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=str(self._meta.path), env=env,
                start_new_session=True,
            )
            handshake = {
                "cpu_sec": self._settings.sandbox_session_cpu_sec,
                "memory_mb": self._settings.sandbox_memory_mb,
                "nofile": self._settings.sandbox_nofile,
                "fsize_mb": self._settings.sandbox_fsize_mb,
                "kind": self._meta.kind,
                "indicators": indicator_specs,
            }
            # [indicator-consumption-wiring] §2.4 (codex plan r1 M5):
            # `outputs` は **kind == "indicator" のときだけ**載せる契約
            # (設計書 §2.4 / codex 設計 r7 M1)。辞書リテラルに直接書くと
            # strategy / signal にもキー自体 (`null`) が届き、契約が
            # 「常に存在する nullable キー」に変質する。**分岐で足す**。
            if self._meta.kind == "indicator":
                handshake["outputs"] = (list(self._meta.outputs)
                                        if self._meta.outputs is not None
                                        else None)
            self._write_line(handshake)

            response = self._read_response(_STARTUP_TIMEOUT_SEC, _STARTUP_MAX_BYTES)
            if not response.get("ok"):
                raise SandboxError(
                    f"plugin worker failed to start: {response.get('error')}")
            self.pid = response.get("pid")
            return self
        except SandboxError:
            self._dead = True
            self.close()
            raise
        except Exception as exc:
            self._dead = True
            self.close()
            raise SandboxError(f"failed to start plugin worker: {exc}") from exc

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        self.close()
        return False

    def close(self) -> None:
        """graceful close (設計書 §2.4): worker が生きていれば
        `{"op": "close"}` を送り `{"ok": true, "cpu_sec": ...}` を
        `sandbox_timeout_sec` 以内で待つ。応答が来れば `cpu_sec` が確定し、
        来なければ従来どおり SIGKILL (`cpu_sec` は None のまま)。"""
        self._check_owner_thread()
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            if not self._dead:
                try:
                    self._write_line({"op": "close"})
                    response = self._read_response(
                        self._settings.sandbox_timeout_sec, _STARTUP_MAX_BYTES)
                    if response.get("ok"):
                        value = response.get("cpu_sec")
                        if isinstance(value, (int, float)) and not isinstance(
                                value, bool):
                            self._cpu_sec = float(value)
                    try:
                        proc.wait(timeout=self._settings.sandbox_timeout_sec)
                    except subprocess.TimeoutExpired:
                        pass
                except (SandboxError, OSError):
                    # timeout/EOF/書き込み失敗 — fallback で kill する
                    # (`_read_response` は失敗時に自分で _kill する)。
                    self._cpu_sec = None
            if proc.poll() is None:
                self._kill()
        # 不変条件: この時点で proc は必ず終了済み (`_kill()` が
        # `wait()` まで済ませている)。`_kill()` が実効性を失う変異を
        # 注入すると `proc.stdout.close()` が **デッドロックする**
        # (別スレッドの `_reader_worker` が `read1()` でブロックしたまま
        # 戻ってこない — worker がまだ生きて出力を出さない限り解放され
        # ない。レビュー fix round 1 F3 の変異テストで実際に踏んだ経路:
        # `_kill()` no-op → ここで停止 → RLIMIT_CPU の SIGXCPU で worker
        # が自滅し EOF が出るまで解けなかった)。順序 (`_kill()` を必ず
        # 先に完了させる) を変えないこと。
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        self._proc = None

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

        try:
            self._write_line(request)
        except SandboxError:
            # `_write_line` 内の json.dumps 失敗 (payload に numpy スカラー
            # 等シリアライズ不能な値が混入) — 実際にはまだ何もパイプへ
            # 書き込んでいないため、セッションは継続利用可能 (レビュー
            # fix round 1 F5)。
            raise
        except OSError as exc:
            self._dead = True
            self._kill()
            raise SandboxError(f"failed to write to plugin worker: {exc}") from exc

        response = self._read_response(self._settings.sandbox_timeout_sec,
                                       self._settings.sandbox_output_max_bytes)
        self.pid = response.get("pid", self.pid)
        if not response.get("ok"):
            raise SandboxError(str(response.get("error", "unknown plugin error")))

        result = response.get("result")
        if kind == "indicator":
            return _validate_indicator_result(result)
        if kind == "signal":
            return {"signals": _validate_signal_result(result)}
        return _validate_strategy_result(result)

    # -- IPC 詳細 --

    def _write_line(self, obj: dict[str, Any]) -> None:
        """1 行書き込む。**例外の型で「シリアライズ失敗 (書き込み前、
        セッション継続可)」と「I/O 失敗 (壊れたパイプ、セッション死亡)」
        を呼び出し元が区別できるようにする** (レビュー fix round 1 F5):
        `json.dumps` が `TypeError`/`ValueError` (payload に numpy スカラー
        等シリアライズ不能な値が混入) を出したら `SandboxError` として
        送出し、実際のパイプ書き込みで失敗したら `OSError` をそのまま
        伝播させる (呼び出し元の `call()`/`__enter__` が使い分ける)。
        """
        assert self._proc is not None and self._proc.stdin is not None
        try:
            data = json.dumps(obj).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise SandboxError(f"failed to serialize request: {exc}") from exc
        self._proc.stdin.write(data)
        self._proc.stdin.flush()

    def _read_response(self, timeout_sec: float, max_bytes: int) -> dict[str, Any]:
        """1 行を「timeout・出力上限超過・EOF」いずれかに達するまで別
        スレッドで読み、結果を待つ。**出力を無制限にバッファしない**
        (`max_bytes` を超えた時点で読み取りを打ち切る)。timeout/oversize/
        EOF はすべてセッションを使用不能にする。`max_bytes` は呼び出し元
        が渡す — 起動応答 (ready) は worker 自身が生成する小さな固定文言
        なので `_STARTUP_MAX_BYTES` を使い、call() の応答は
        `settings.sandbox_output_max_bytes` (plugin コードの出力なので
        こちらは利用者設定に従う) を使う、と使い分けるため。
        """
        assert self._proc is not None and self._proc.stdout is not None
        result_queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        # daemon スレッド: 親が timeout で諦めた後もこのスレッドは stdout
        # を読み続け得るが、その後 kill するため程なく EOF で自然終了する
        # (kill せず放置すると次の call() のバイト列を盗み読みしかねない
        # ため、timeout 時は必ず kill してセッションを使用不能にする)。
        t = threading.Thread(target=_reader_worker,
                             args=(self._proc.stdout, max_bytes, result_queue),
                             daemon=True)
        t.start()
        try:
            kind, payload = result_queue.get(timeout=timeout_sec)
        except queue.Empty:
            self._dead = True
            self._kill()
            raise SandboxError(f"plugin call timed out after {timeout_sec}s")

        if kind == "line":
            try:
                return json.loads(payload)
            except json.JSONDecodeError as exc:
                self._dead = True
                self._kill()
                raise SandboxError(
                    f"plugin worker returned invalid JSON: {exc}") from exc
        if kind == "eof":
            self._dead = True
            self._kill()
            raise SandboxError("plugin worker exited unexpectedly (EOF)")
        if kind == "oversize":
            self._dead = True
            self._kill()
            raise SandboxError(
                f"plugin worker output exceeded {max_bytes} bytes")
        # kind == "error"
        self._dead = True
        self._kill()
        raise SandboxError(f"error reading plugin worker output: {payload}")

    def _kill(self) -> None:
        if self._proc is None:
            return
        try:
            # start_new_session=True によりセッションリーダーの pid ==
            # プロセスグループ id。killpg で孫プロセスも道連れにする。
            os.killpg(self._proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _reader_worker(stream: Any, max_bytes: int,
                   result_queue: "queue.Queue[tuple[str, Any]]") -> None:
    """境界の意味論 (レビュー fix round 1 F7 で明文化):
    累積バイト数が **`max_bytes` ちょうど** なら許容 (`>` であって `>=`
    ではない)、1 バイトでも超えたら oversize。判定は `read1()` の
    チャンク到着ごとに行う — 1 行がまだ完成していなくても、蓄積量が
    その時点で上限を超えていれば直ちに打ち切る (改行の到着を待たない)。
    そのため、最終的な行の総バイト数が結果的に上限以内に収まる場合
    でも、チャンク分割の途中経過で一時的に上限を超えていれば oversize
    と判定され得る (「超過検知は次チャンク到着時」の意味論 — 出力を
    無制限にバッファしないためのトレードオフ)。
    """
    buf = bytearray()
    try:
        while True:
            chunk = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not chunk:
                result_queue.put(("eof", None))
                return
            buf.extend(chunk)
            # サイズ判定を改行検出より先に行う: worker の出力が小さければ
            # 1 回の read1() で改行まで丸ごと届くことがあり、「改行が
            # 見つかったら先に line を確定」の順だと、その 1 チャンクの
            # 中身がどれだけ大きくても上限チェックを素通りしてしまう。
            if len(buf) > max_bytes:
                result_queue.put(("oversize", None))
                return
            nl = buf.find(b"\n")
            if nl != -1:
                result_queue.put(("line", bytes(buf[:nl])))
                return
    except Exception as exc:  # noqa: BLE001 — スレッド境界を越えて報告する
        try:
            result_queue.put(("error", exc))
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
        # と、0/負値が `queue.Queue.get(timeout=...)` まで届いてから生の
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

def _validate_indicator_result(result: Any) -> dict[str, float]:
    if not isinstance(result, dict):
        raise SandboxError(f"indicator must return a dict, got {type(result).__name__}")
    out: dict[str, float] = {}
    for key, value in result.items():
        if not isinstance(key, str):
            raise SandboxError(f"indicator result keys must be str, got {key!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SandboxError(f"indicator result[{key!r}] must be a number, got {value!r}")
        fvalue = float(value)
        if not math.isfinite(fvalue):
            raise SandboxError(f"indicator result[{key!r}] must be finite, got {value!r}")
        out[key] = fvalue
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
        strength = float(strength)
        if not math.isfinite(strength) or not (0.0 <= strength <= 1.0):
            raise SandboxError(f"signal[{i}].strength must be in [0, 1], got {strength!r}")

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
                    or not math.isfinite(value) or value <= 0):
                raise SandboxError(
                    f"signal[{i}].{opt} must be a finite positive number, got {value!r}")
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
    except (TypeError, ValueError) as exc:
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
