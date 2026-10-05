"""plugin サンドボックスの子プロセス本体。

`python -B -m agentic_fx.plugin.worker <plugin_dir>` として起動される。
`sandbox.PluginSession` が spawn する唯一の想定呼び出し元。

**起動は二段** (plugin を 1 行も読む前に自分自身を隔離する):

1. `worker_isolation.run_isolation_stage`: bytecode の書き出しを止め、protocol 用に
   stdout を退避して fd 1 を stderr へ向け、handshake を読み、rlimit → 継承 seccomp
   filter の検査 → 匿名 keyring → Landlock → seccomp → sandbox 下の runtime import
   自己試験を行う。失敗はそこで固定の `sandbox_ready ok:false` を書いて終了する。
2. 成功したら `sandbox_ready` (attestation) を書き、親の `{"op": "load"}` を待つ。
   EOF や別の行なら plugin を読まずに終了する。
3. `load` を受けたら `socket` 等を塞ぎ、source-only loader で main と依存 indicator
   を読み (hash を照合した同じ bytes を exec する)、`plugin_ready` を返す。
4. 以後は stdin から JSON 1 行を読むたびに kind の関数 (compute/detect/evaluate) を
   呼び、結果を JSON 1 行で返す。`{"op": "close"}` で `cpu_sec` を添えて応答し終了する。

**worker はこのプロセス自身が信頼境界の内側寄りの実行体である** — kind 別の戻り値
スキーマ検証は親 (`sandbox.py`) の責務。ただし同居 indicator の compute 結果は
`core.plugin_contract.validate_indicator_result` を worker 内でも通す (strategy に
渡す前に不正な indicator 出力を弾くため)。

**ワイヤ形式は `sandbox.py` と対の契約** (変更する場合は両方のモジュール docstring を
同期すること):

- handshake (最初の 1 行、親から):
  `{"cpu_sec": int, "memory_mb": int, "nofile": int, "fsize_mb": int,
    "attest_nonce": "<32 hex>", "content_hash": "<64 hex>",
    "kind": "indicator"|"signal"|"strategy",
    "outputs": [...]|null,          # kind == "indicator" のときのみ存在
    "indicators": [{"alias","plugin_py","content_hash","params","max_bars","outputs"}]}
- sandbox_ready (worker から): 成功は
  `{"phase": "sandbox_ready", "ok": true, "pid": int, <attested field 8 個>}`、
  失敗は `{"phase": "sandbox_ready", "ok": false, "stage": "sandbox", "reason": ..., "pid": int}`
- load (親から、検証に通ったときだけ): `{"op": "load"}`
- plugin_ready (worker から): `{"phase": "plugin_ready", "ok": true}` /
  `{"phase": "plugin_ready", "ok": false, "error": "<message>"}`
- request (call() のたびに親から): `{"df": <df wire>, "params": {...}, "id": "<hex>"}`
  df wire: `{"index": [iso8601 str, ...], "open": [...], "high": [...],
            "low": [...], "close": [...], "volume": [...]}`
- response: `{"ok": true, "result": <plugin の生の戻り値>, "pid": int, "id": <要求の id>}` /
  `{"ok": false, "error": "<message>", "pid": int, "id": <要求の id>}`
- close request: `{"op": "close", "id": "<hex>"}` / close response:
  `{"ok": true, "cpu_sec": <float>, "pid": int, "id": <要求の id>}`

load の後に届く行は plugin が protocol fd へ直接書き得る。親はそれらを候補が制御
し得る入力として扱い、ここでの整形には頼らない。

起動失敗と call 中の例外は、protocol の応答より前に traceback を stderr に書く
(親は stderr を人間向けの技術ログにだけ出す)。
"""
from __future__ import annotations

import json
import os
import resource
import sys
import traceback
from typing import Any

from agentic_fx.core.worker_limits import NPROC_CAP as _NPROC_CAP
from agentic_fx.plugin import worker_isolation

# `_NPROC_CAP` は `core.worker_limits` が単一の出所。gate pytest worker と plugin
# worker (隔離段) が同じ値を使う。この別名は既存の import 元を保つためのもの

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


def _deep_copy_json(obj):
    """params の deep copy (call ごとに独立。nested mutation を次 call へ
    持ち越さない — codex r3 C3)。params は JSON-safe が loader/resolver で
    保証済みなので json 往復で足りる。"""
    return json.loads(json.dumps(obj))


def _cpu_sec() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return float(usage.ru_utime + usage.ru_stime)


_KIND_FUNC = {"indicator": "compute", "signal": "detect", "strategy": "evaluate"}


def _wire_to_df(wire: dict[str, Any]):
    import pandas as pd

    index = pd.to_datetime(wire["index"], utc=True)
    data = {col: wire[col] for col in ("open", "high", "low", "close", "volume")}
    return pd.DataFrame(data, index=index)


def _indicator_result_to_wire(validated: dict) -> dict:
    """standalone (`get_indicators`) 応答の wire 表現。
    スカラーは float、系列は `{"series": [float|null, ...]}`。
    NaN は `null` にしてから送る (親は allow_nan=False で読める形)。

    `validated` は `validate_indicator_result(..., df_index=...)` の戻り値で、
    系列は必ず `pd.Series` に畳まれている (素の list は入らない)。"""
    import math

    import pandas as pd

    out: dict = {}
    for key, value in validated.items():
        if isinstance(value, pd.Series):
            out[key] = {"series": [None if pd.isna(v) else float(v)
                                   for v in value.tolist()]}
        else:
            out[key] = None if math.isnan(float(value)) else float(value)
    return out


def _read_line() -> dict[str, Any] | None:
    line = sys.stdin.buffer.readline()
    if not line:
        return None
    return json.loads(line)


def _write_line(fd: int, obj: dict[str, Any]) -> None:
    data = json.dumps(obj, allow_nan=False).encode("utf-8") + b"\n"
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        view = view[n:]


def _load_error_text(exc: BaseException) -> str:
    cause = exc.__cause__
    if isinstance(exc, worker_isolation.PluginLoadError) and cause is not None:
        return f"{exc.reason}: {type(cause).__name__}: {cause}"
    return f"{type(exc).__name__}: {exc}"


def _print_load_traceback(exc: BaseException) -> None:
    """読み込み失敗の traceback を stderr に書く。loader の失敗は元の例外
    (`__cause__`、plugin の行を含む) を出す。"""
    shown = exc.__cause__ if exc.__cause__ is not None else exc
    try:
        sys.stderr.write("plugin worker: plugin load failed\n")
        traceback.print_exception(shown, file=sys.stderr)
        sys.stderr.flush()
    except Exception:  # noqa: BLE001  診断が書けなくても応答は返す
        pass


def _load_plugins(isolated: "worker_isolation.IsolatedWorker",
                  handshake: dict[str, Any]):
    kind = handshake["kind"]
    module = worker_isolation.load_plugin_module(isolated.main)
    fn = getattr(module, _KIND_FUNC[kind])
    specs = {spec["alias"]: spec for spec in handshake.get("indicators") or []}
    deps = []
    for record in isolated.indicators:
        spec = specs[record.alias]
        dep_module = worker_isolation.load_plugin_module(record)
        deps.append({
            "alias": record.alias, "compute": getattr(dep_module, "compute"),
            "params": spec["params"], "max_bars": int(spec["max_bars"]),
            "outputs": tuple(spec["outputs"])})
    return fn, deps


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python -m agentic_fx.plugin.worker <plugin_dir>")
    plugin_dir = sys.argv[1]

    captured: dict[str, Any] = {}

    def read_handshake() -> dict[str, Any] | None:
        handshake = _read_line()
        captured["handshake"] = handshake
        return handshake

    isolated = worker_isolation.run_isolation_stage(plugin_dir, read_handshake)
    if isolated is None:
        return
    handshake = captured["handshake"]
    protocol_fd = isolated.protocol_fd
    pid = os.getpid()
    os.write(protocol_fd, isolated.sandbox_ready_line(pid))

    # 親が attestation と /proc を確かめて load を送るまで plugin を読まない
    try:
        load = _read_line()
    except ValueError:
        load = None
    if load != {"op": "load"}:
        isolated.close()
        return

    _poison_network_modules()
    try:
        fn, deps = _load_plugins(isolated, handshake)
    except Exception as exc:  # noqa: BLE001  候補側の読み込み失敗を構造化して返す
        _print_load_traceback(exc)
        isolated.close()
        _write_line(protocol_fd, {"phase": "plugin_ready", "ok": False,
                                  "error": _load_error_text(exc)})
        return
    isolated.close()
    _write_line(protocol_fd, {"phase": "plugin_ready", "ok": True})

    kind = handshake["kind"]
    main_outputs = handshake.get("outputs") if kind == "indicator" else None

    import numpy as np
    import pandas as pd

    from agentic_fx.core.plugin_contract import validate_indicator_result

    baseline_chained = pd.get_option("mode.chained_assignment")
    baseline_errstate = dict(np.geterr())

    while True:
        request = _read_line()
        if request is None:
            return
        request_id = request.get("id")
        if request.get("op") == "close":
            _write_line(protocol_fd, {"ok": True, "cpu_sec": _cpu_sec(), "pid": pid,
                                      "id": request_id})
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
                # indicator が pandas/numpy の process 全体の状態を変えていないこと
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
            _write_line(protocol_fd, {"ok": True, "result": result, "pid": pid,
                                      "id": request_id})
        except Exception as exc:  # noqa: BLE001 — 構造化 1 行で返し、traceback は stderr へ
            try:
                traceback.print_exc(file=sys.stderr)
                sys.stderr.flush()
            except Exception:  # noqa: BLE001
                pass
            _write_line(protocol_fd, {"ok": False,
                        "error": f"{type(exc).__name__}: {exc}", "pid": pid,
                        "id": request_id})


if __name__ == "__main__":
    main()
