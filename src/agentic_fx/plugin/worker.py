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
3. `plugin.py` (main kind) と、strategy の場合は同居する indicator の
   `plugin.py` 群を、それぞれ一意なモジュール名で import する (以後、
   同じモジュールオブジェクトを全 call で使い回す — セッション型 IPC の要)
4. stdin から JSON 1 行を読むたびに、main kind に対応する関数
   (compute/detect/evaluate) を呼ぶ。strategy の場合は呼ぶ前に同居
   indicator を `compute` → 共通 validator → 親 df.index へ整列した
   `indicators` 引数を組み立てる。結果を JSON 1 行で返す
5. `{"op": "close"}` を受け取ったら `cpu_sec` を添えて応答し終了する
   (graceful close)

**worker はこのプロセス自身が信頼境界の内側寄りの実行体である** —
kind 別の戻り値スキーマ検証 (`bar_ts` 拒否、`StrategyDecision` 構築等)
は一切ここでは行わない。すべて親プロセス (`sandbox.py`) の責務。**ただし
indicator の戻り値検証だけは例外** — 同居 indicator の compute 結果は
`core.plugin_contract.validate_indicator_result` (worker/sandbox の両方が
import する唯一の実装) を worker 内でも通す。strategy に渡す前に
不正な indicator 出力を弾く必要があるため。worker は plugin が返した
生の値をそのまま JSON にして返すだけであり、JSON 化に失敗する値
(numpy スカラー等、plugin 作者が `float()` で明示変換し忘れた場合) は
ここで構造化エラーとして報告する。

**ワイヤ形式は `sandbox.py` と対の契約** (変更する場合は両方のモジュール
docstring を同期すること):

- handshake (最初の 1 行、親から):
  `{"cpu_sec": int, "memory_mb": int, "nofile": int, "fsize_mb": int,
    "kind": "indicator"|"signal"|"strategy",
    "outputs": [...]|null,          # kind == "indicator" のときのみ存在
    "indicators": [{"alias","plugin_py","params","max_bars","outputs"}]}
  (`indicators` は strategy のみ非空。indicator/signal は常に `[]`)
- ready (handshake への応答、plugin.py の import 成功後に送る):
  `{"ok": true, "ready": true, "pid": int}` /
  `{"ok": false, "ready": false, "error": "<message>"}`
- request (call() のたびに親から):
  `{"df": <df wire>, "params": {...}}` (indicator/signal/strategy 共通。
  strategy の `indicators`/`signals` は worker 内で計算・`None` 固定に
  なったため、もう request には乗らない)
  df wire: `{"index": [iso8601 str, ...], "open": [...], "high": [...],
            "low": [...], "close": [...], "volume": [...]}`
  (float は JSON 往復で bit-exact — CPython の shortest-roundtrip repr)
- close request (親から、セッション終了時): `{"op": "close"}`
- close response: `{"ok": true, "cpu_sec": <float>, "pid": int}`
- response (request 1 件につき 1 行):
  `{"ok": true, "result": <plugin の生の戻り値>, "pid": int}` /
  `{"ok": false, "error": "<message>", "pid": int}`
  kind == "indicator" の main plugin 応答の `result` は standalone wire
  形式 (`{key: float|null | {"series": [float|null, ...]}}`)。strategy の
  `result` は `evaluate()` の生の戻り値 (スキーマ検証は親側)。

脅威モデルは `sandbox.py` のモジュール docstring を参照 (構文名ベースの
静的検査・resource limit は善意の plugin コードの事故防止が目的であり、
悪意ある攻撃者からの完全な隔離を保証しない。最終防衛線は人間承認)。
"""
from __future__ import annotations

import json
import os
import resource
import sys
from typing import Any

# RLIMIT_NPROC の固定上限。**per-uid の累積カウンタ**なので、値が小さす
# ぎると「同一 uid が既に多数のプロセス/スレッドを持つ」通常の開発機
# (常態) で worker 自身の起動が壊れる — レビュー fix round 1 F6:
# 当初 32 だったが、これは典型的な開発機の同時プロセス/スレッド数を軽く
# 下回り、正常な plugin 実行まで巻き込んで失敗させていた (setrlimit
# 自体は成功するが、以後 clone()/pthread_create() が即座に失敗する)。
# fork bomb 事故防止という目的に対しては十分に寛大な値で足りる —
# plugin.py が subprocess/os/threading を import すること自体が
# check_source の allowlist (math/statistics/numpy/pandas のみ) で既に
# 拒否されており plugin コードが自発的に fork/thread を増やす経路が無い
# のと、worker 起動時に env で BLAS/OpenMP をシングルスレッド化している
# (sandbox._SINGLE_THREAD_ENV) ため worker 自身が新規スレッドをほぼ必要
# としないため、512 という寛大な値でも fail closed の実効性は変わらない
# (RLIMIT_CPU/RLIMIT_AS が主防御、RLIMIT_NPROC はベストエフォートの追加
# 防御という位置づけも変更なし)。
_NPROC_CAP = 512


def _set_resource_limits(cpu_sec: int, memory_mb: int, nofile: int,
                          fsize_mb: int) -> None:
    cpu = int(cpu_sec)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))

    mem_bytes = int(memory_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (mem_bytes, mem_bytes))

    # プラン 8 B 束: worker は plugin.py の import と call() の応答書き込み
    # 以外にファイル記述子を要しない (stdin/stdout/stderr の 3 つ +
    # import 時の一時的な .so/.pyc オープン)。想定外の大量オープン
    # (fork bomb 的 fd リーク) を検知する上限として十分寛大な値を渡す
    # (呼び出し元が settings.plugin.sandbox_nofile を渡す — 既定 128)。
    resource.setrlimit(resource.RLIMIT_NOFILE, (int(nofile), int(nofile)))

    # RLIMIT_FSIZE: plugin コードは check_source の denylist
    # (open/to_*/read_* 等) により意図的なファイル書き込みができない —
    # ここでの上限は「想定外の書き込みを小さく抑える」多層防御 (呼び出し
    # 元が settings.plugin.sandbox_fsize_mb を渡す — 既定 8MB)。
    fsize_bytes = int(fsize_mb) * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))

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


def _import_plugin_as(module_name: str, plugin_path: str):
    """`plugin.py` を **一意なモジュール名**で import する。strategy と
    同居する indicator を `"plugin"` 固定名で import すると sys.modules が
    衝突して 2 本目以降が 1 本目に化ける。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location(module_name, plugin_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin module from {plugin_path!r}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _import_plugin(plugin_dir: str):
    return _import_plugin_as("plugin", os.path.join(plugin_dir, "plugin.py"))


def _deep_copy_json(obj):
    """params の deep copy (call ごとに独立。nested mutation を次 call へ
    持ち越さない — codex r3 C3)。params は JSON-safe が loader/resolver で
    保証済みなので json 往復で足りる。"""
    return json.loads(json.dumps(obj))


def _cpu_sec() -> float:
    import resource as _res
    usage = _res.getrusage(_res.RUSAGE_SELF)
    return float(usage.ru_utime + usage.ru_stime)


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
    stream.write(json.dumps(obj, allow_nan=False).encode("utf-8") + b"\n")
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


def _indicator_result_to_wire(validated: dict) -> dict:
    """standalone (`get_indicators`) 応答の wire 表現。
    スカラーは float、系列は `{"series": [float|null, ...]}`。
    NaN は `null` にしてから送る (親は allow_nan=False で読める形)。

    2 周目 ローカル LLM (c03 muse [Critical] / c03 ornith [Important] /
    c03 qwen [Minor] — 独立 3 本が同じ行に到達、2026-09-18):
    `list` 分岐の NaN 正規化が `v is None` だけで、`float('nan')` が
    素通りする形になっていた。**実害は無い** — 唯一の呼び出し元
    (`main()` の `kind == "indicator"` 分岐) は
    `validate_indicator_result(result, df_index=df.index, ...)` の戻り値を
    渡し、`df_index` が非 None のとき共通 validator は list/tuple/ndarray を
    **必ず `pd.Series` へ畳む** (probe 実測) ので、`validated` に素の `list`
    は入らない = この分岐は現状 到達不能。ただし到達すれば `nan` が
    `_write_line` の JSON に載り、親の `allow_nan=False` 読み取りが落ちる
    という罠なので、`pd.Series` 分岐と同じ判定に揃えておく (到達可能な
    経路の挙動は変えない)。"""
    import math

    import pandas as pd

    out: dict = {}
    for key, value in validated.items():
        if isinstance(value, pd.Series):
            out[key] = {"series": [None if pd.isna(v) else float(v)
                                   for v in value.tolist()]}
        elif isinstance(value, list):
            out[key] = {"series": [None if v is None or pd.isna(v) else float(v)
                                   for v in value]}
        else:
            out[key] = None if math.isnan(float(value)) else float(value)
    return out


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
        _set_resource_limits(handshake["cpu_sec"], handshake["memory_mb"],
                              handshake["nofile"], handshake["fsize_mb"])
        _poison_network_modules()
        plugin_module = _import_plugin(plugin_dir)
        kind = handshake["kind"]
        func_name = _KIND_FUNC[kind]
        fn = getattr(plugin_module, func_name)
        main_outputs = handshake.get("outputs") if kind == "indicator" else None
        # 同居 indicator (strategy のみ非空)。alias 順で来る。
        deps = []
        for spec in handshake.get("indicators") or []:
            module = _import_plugin_as(f"indicator_{spec['alias']}",
                                       spec["plugin_py"])
            deps.append({
                "alias": spec["alias"], "compute": getattr(module, "compute"),
                "params": spec["params"], "max_bars": int(spec["max_bars"]),
                "outputs": tuple(spec["outputs"])})
    except Exception as exc:  # noqa: BLE001 — 起動失敗を構造化エラーで報告
        _write_line(protocol_out, {"ok": False, "ready": False,
                    "error": f"{type(exc).__name__}: {exc}", "pid": pid})
        return

    _write_line(protocol_out, {"ok": True, "ready": True, "pid": pid})

    import pandas as pd
    import numpy as np
    from agentic_fx.core.plugin_contract import validate_indicator_result

    baseline_chained = pd.get_option("mode.chained_assignment")
    baseline_errstate = dict(np.geterr())

    while True:
        request = _read_line()
        if request is None:
            return
        # graceful close (設計書 §2.4): 親が {"op": "close"} を送る。
        if request.get("op") == "close":
            _write_line(protocol_out,
                        {"ok": True, "cpu_sec": _cpu_sec(), "pid": pid})
            return
        try:
            df = _wire_to_df(request["df"])
            params = _deep_copy_json(request["params"])
            if kind == "strategy":
                indicators = {}
                for dep in deps:
                    sub_df = df.tail(dep["max_bars"]).copy(deep=True)
                    raw = dep["compute"](sub_df, _deep_copy_json(dep["params"]))
                    validated = validate_indicator_result(
                        raw, df_index=sub_df.index, outputs=dep["outputs"])
                    indicators[dep["alias"]] = {
                        key: (value.reindex(df.index)
                              if isinstance(value, pd.Series) else value)
                        for key, value in validated.items()}
                # グローバル状態の不変 assert (設計書 §2.4、V2)
                if (pd.get_option("mode.chained_assignment") != baseline_chained
                        or dict(np.geterr()) != baseline_errstate):
                    raise RuntimeError(
                        "indicator mutated shared pandas/numpy global state")
                result = fn(df, indicators, None, params)
            else:
                result = fn(df, params)
                if kind == "indicator":
                    result = _indicator_result_to_wire(
                        validate_indicator_result(result, df_index=df.index,
                                                  outputs=main_outputs))
            _write_line(protocol_out, {"ok": True, "result": result, "pid": pid})
        except Exception as exc:  # noqa: BLE001 — トレースバックを stdout
            # プロトコルに乗せない (worker-side エラーは構造化 1 行で
            # 報告する — brief 「never tracebacks to stdout mid-protocol」)。
            _write_line(protocol_out, {"ok": False,
                        "error": f"{type(exc).__name__}: {exc}", "pid": pid})


if __name__ == "__main__":
    main()
