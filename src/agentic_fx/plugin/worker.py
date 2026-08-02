"""plugin サンドボックスの子プロセス本体 (プラン 7 Task 2)。

`python -m agentic_fx.plugin.worker <plugin_dir>` として起動される。
`sandbox.PluginSession` が spawn する唯一の想定呼び出し元。

**起動順序は厳守** (`resource.setrlimit` を通した後でないと `import
pandas` 自体が plugin コードと同じ資源制約の外で走ってしまう):

0. 元の stdout fd を複製して退避し、fd 1 を stderr へ付け替える
   (`_protect_protocol_stdout` — plugin.py 内の `print` 等が JSON-lines
   プロトコルに混ざるのを防ぐ。handshake を読む前、他の何より先に行う)
1. handshake (最初の 1 行) を読み、`resource.setrlimit` で CPU 時間
   (RLIMIT_CPU, セッション寿命累積)・仮想アドレス空間 (RLIMIT_AS)・
   プロセス/スレッド数 (RLIMIT_NPROC) の上限を設定する
2. `socket`/`urllib`/`http` を `sys.modules` にダミー登録して塞ぐ
   (plugin.py が import する前に、でなければ意味が無い)
3. `plugin.py` を 1 回だけ import する (以後、同じモジュールオブジェクト
   を全 call で使い回す — セッション型 IPC の要)
4. stdin から JSON 1 行を読むたびに、handshake で指定された kind に対応
   する関数 (compute/detect/evaluate) を呼び、結果を JSON 1 行で返す

**worker はこのプロセス自身が信頼境界の内側寄りの実行体である** —
kind 別の戻り値スキーマ検証 (`bar_ts` 拒否、`StrategyDecision` 構築等)
は一切ここでは行わない。すべて親プロセス (`sandbox.py`) の責務。worker
は plugin が返した生の値をそのまま JSON にして返すだけであり、JSON化
に失敗する値 (numpy スカラー等、plugin 作者が `float()` で明示変換し
忘れた場合) はここで構造化エラーとして報告する。

**ワイヤ形式は `sandbox.py` と対の契約** (変更する場合は両方のモジュール
docstring を同期すること):

- handshake (最初の 1 行、親から):
  `{"cpu_sec": int, "memory_mb": int, "kind": "indicator"|"signal"|"strategy"}`
- ready (handshake への応答、plugin.py の import 成功後に送る):
  `{"ok": true, "ready": true, "pid": int}` /
  `{"ok": false, "ready": false, "error": "<message>"}`
- request (call() のたびに親から):
  kind=indicator/signal → `{"df": <df wire>, "params": {...}}`
  kind=strategy         → `{"df": <df wire>, "indicators": {...},
                            "signals": [...], "params": {...}}`
  df wire: `{"index": [iso8601 str, ...], "open": [...], "high": [...],
            "low": [...], "close": [...], "volume": [...]}`
  (float は JSON 往復で bit-exact — CPython の shortest-roundtrip repr)
- response (request 1 件につき 1 行):
  `{"ok": true, "result": <plugin の生の戻り値>, "pid": int}` /
  `{"ok": false, "error": "<message>", "pid": int}`

脅威モデルは `sandbox.py` のモジュール docstring を参照 (構文名ベースの
静的検査・resource limit は善意の plugin コードの事故防止が目的であり、
悪意ある攻撃者からの完全な隔離を保証しない。最終防衛線は人間承認)。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

# RLIMIT_NPROC の固定上限。**per-uid の累積カウンタ**なので、実運用の
# 開発機では既にこの値をとうに超えたプロセス/スレッド数が同一 uid 配下
# に存在するのが普通であり、その場合 worker は新規スレッドを 1 つも
# 作れない (setrlimit 自体は成功するが、以後の clone()/pthread_create()
# が即座に失敗する) — つまり「まだ余裕がある」保証は無い。それでも安全
# なのは、plugin.py が subprocess/os/threading を import すること自体が
# check_source の allowlist (math/statistics/numpy/pandas のみ) で既に
# 拒否されており plugin コードが自発的に fork/thread を増やす経路が無い
# のと、worker 起動時に env で BLAS/OpenMP をシングルスレッド化している
# (sandbox._SINGLE_THREAD_ENV) ため worker 自身が新規スレッドを必要と
# する場面もほぼ無いため。RLIMIT_NPROC はあくまでベストエフォートの追加
# 防御であり、fail closed の主防御は RLIMIT_CPU/RLIMIT_AS の 2 軸。
_NPROC_CAP = 32


def _set_resource_limits(cpu_sec: int, memory_mb: int) -> None:
    import resource

    cpu = int(cpu_sec)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    mem_bytes = int(memory_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (_NPROC_CAP, _NPROC_CAP))
    except (ValueError, OSError):
        # per-uid の既存使用量次第では失敗し得る (最終防衛線ではなく
        # ベストエフォートの追加防御 — CPU/AS の 2 軸が主防御)。
        pass


class _BlockedModule:
    """import 済み扱いにして `ImportError` を強制する偽モジュール。"""

    def __getattr__(self, name: str) -> Any:
        raise ImportError(
            "network access is not allowed in the plugin sandbox "
            f"(blocked attribute: {name!r})")


def _poison_network_modules() -> None:
    """socket/urllib/http のうち、実際にネットワーク I/O 能力を持つ
    サブモジュールだけを塞ぐ。**`urllib`/`urllib.parse`/`urllib.error`/
    `http` の親パッケージそのものは塞がない** — `pandas` は import 時点
    で `pandas.io.common` が `from urllib.parse import ...` を無条件に
    実行する (URL 文字列判定の純粋な文字列処理で、ネットワークには一切
    触れない) ため、ここを塞ぐと `import pandas` 自体が壊れる (実測で
    確認済み)。`urllib.error` は例外クラス定義のみで同様に無害。実際に
    ネットワーク I/O を行えるのは `socket`・`urllib.request`
    (`urlopen`)・`http.client` (`HTTPConnection`)・`http.server` のみ —
    この 4 つを塞げば十分 (check_source の import allowlist が
    socket/urllib/http のトップレベル import 自体を既に AST 検査で拒否
    しているため、この poison はあくまで多層防御)。
    """
    for name in ("socket", "urllib.request", "http.client", "http.server"):
        sys.modules[name] = _BlockedModule()  # type: ignore[assignment]


def _import_plugin(plugin_dir: str):
    import importlib.util

    plugin_path = os.path.join(plugin_dir, "plugin.py")
    spec = importlib.util.spec_from_file_location("plugin", plugin_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin module from {plugin_path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_KIND_FUNC = {"indicator": "compute", "signal": "detect", "strategy": "evaluate"}


def _wire_to_df(wire: dict[str, Any]):
    import pandas as pd

    index = pd.to_datetime(wire["index"], utc=True)
    data = {col: wire[col] for col in ("open", "high", "low", "close", "volume")}
    return pd.DataFrame(data, index=index)


def _read_line() -> dict[str, Any] | None:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line)


def _write_line(stream: Any, obj: dict[str, Any]) -> None:
    stream.write(json.dumps(obj).encode("utf-8") + b"\n")
    stream.flush()


def _protect_protocol_stdout() -> Any:
    """JSON-lines プロトコル専用の書き込み先を確保し、fd 1 (stdout) を
    fd 2 (stderr) へ付け替えて返す。

    `check_source` の denylist に `print` は含まれない (brief 逐語リスト
    に無く、善意の plugin がデバッグ用に残しがちな呼び出しを一律禁止する
    のは過剰) — その代わり、plugin.py 内の `print(...)` や C 拡張の
    printf がプロトコルの JSON 1 行ストリームに紛れ込まないよう、
    plugin.py を import する**前**に元の stdout fd を複製して退避し、
    fd 1 自体を stderr (親プロセスは `stderr=subprocess.DEVNULL` で起動
    するため、混入するはずだった出力は静かに捨てられる) へ向け直す。
    以後 `_write_line` は必ずこの複製 fd に書く。
    """
    protocol_out = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    return protocol_out


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agentic_fx.plugin.worker <plugin_dir>")
    plugin_dir = sys.argv[1]

    protocol_out = _protect_protocol_stdout()

    handshake = _read_line()
    if handshake is None:
        return

    pid = os.getpid()
    try:
        _set_resource_limits(handshake["cpu_sec"], handshake["memory_mb"])
        _poison_network_modules()
        plugin_module = _import_plugin(plugin_dir)
        kind = handshake["kind"]
        func_name = _KIND_FUNC[kind]
        fn = getattr(plugin_module, func_name)
    except Exception as exc:  # noqa: BLE001 — 起動失敗を構造化エラーで報告
        _write_line(protocol_out, {"ok": False, "ready": False,
                    "error": f"{type(exc).__name__}: {exc}", "pid": pid})
        return

    _write_line(protocol_out, {"ok": True, "ready": True, "pid": pid})

    while True:
        request = _read_line()
        if request is None:
            return
        try:
            df = _wire_to_df(request["df"])
            if kind == "strategy":
                result = fn(df, request["indicators"], request["signals"],
                           request["params"])
            else:
                result = fn(df, request["params"])
            _write_line(protocol_out, {"ok": True, "result": result, "pid": pid})
        except Exception as exc:  # noqa: BLE001 — トレースバックを stdout
            # プロトコルに乗せない (worker-side エラーは構造化 1 行で
            # 報告する — brief 「never tracebacks to stdout mid-protocol」)。
            _write_line(protocol_out, {"ok": False,
                        "error": f"{type(exc).__name__}: {exc}", "pid": pid})


if __name__ == "__main__":
    main()
