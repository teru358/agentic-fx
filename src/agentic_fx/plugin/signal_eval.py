"""signal plugin の検出精度評価 (プラン 7 Task 4、設計書 §6「測り方は
Phase 2 で定める」の確定)。

plugin フォルダ同梱の `labels.json` (ラベル付きサンプル) を使い、sandbox
経由で `detect` をウォークフォワードに実行して precision/recall を算出
する。`labels.json` スキーマ:
`{"bars": [[iso, o, h, l, c, v], ...], "expected": [{"bar_ts": iso,
"direction": "long"|"short"}, ...]}`。

**ウォークフォワード評価 (コントローラ裁定)**: sandbox の signal 契約
上、`detect` の戻り値は `bar_ts` を含まない (`bar_ts` はハーネス専有の
監査キーであり plugin が設定すると `sandbox.SandboxError` になる —
`sandbox.py` の `_validate_signal_result` を参照)。単発呼び出しでは
複数時点のシグナルを区別できないため、labels.json の各バー時点
`i` (`i = 0..n-1`) について `df.iloc[:i+1].tail(meta.max_bars)` を渡して
`detect` を 1 回ずつ呼び、返った signals に `bar_ts = df.index[i]` を
付与してから `(bar_ts, direction)` の集合として突合する
(`predicted ∩ expected` = tp、`predicted - expected` = fp、
`expected - predicted` = fn。重複は集合演算で自然に 1 件へ潰れる)。

**fail closed (承認フローへの伝播)**: `SandboxError` はここでは catch
しない。`tools.market_tools.get_indicators` の fail-open (運用文脈 —
1 plugin の不調で他の指標まで失わせない) とは**意図的に逆**で、評価
文脈では plugin の失敗 = 評価そのものの失敗であり、承認フロー
(Task 6 の consumer) に fail closed で伝えるべきだから。

**セッション償却**: `sandbox_run` 省略時 (既定 `None`) は
`PluginSession` を 1 個だけ開き、その `.call()` を全バー時点で使い回す
(labels.json は通常数十〜数百バー分あり、バーごとに 1 プロセスを起動し
ていては `sandbox.py` 自身が明記する「大量評価はセッションを使え」に
反する)。`sandbox_run` が注入された場合は `market_tools.build` と同じ
呼び出し規約 `sandbox_run(meta, payload, settings=settings.plugin)` で
バーごとに呼ぶ (テストで fake を差し替えるための注入点 — 実サンドボック
スサブプロセスを起動せずロジックだけを検証できる)。
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Callable, Iterator

import pandas as pd

from agentic_fx.plugin import sandbox as plugin_sandbox
from agentic_fx.plugin.loader import PluginMeta

if TYPE_CHECKING:
    from agentic_fx.config import Settings

_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")
_VALID_DIRECTIONS = frozenset({"long", "short"})
_EXPECTED_ALLOWED_KEYS = frozenset({"bar_ts", "direction"})

# `market_tools.build` と同じ注入シグネチャ: (meta, payload, *, settings)
# → dict。`settings` には `Settings.plugin` (PluginSettings) を渡す。
SandboxRunFn = Callable[..., dict]

PredictedSet = set[tuple[pd.Timestamp, str]]


def evaluate_detection(meta: PluginMeta, *, sandbox_run: SandboxRunFn | None = None,
                       settings: "Settings") -> dict[str, Any]:
    """labels.json のラベル付きサンプルで signal plugin の `detect` を
    ウォークフォワード評価し、`{"precision", "recall", "tp", "fp", "fn",
    "n_labels"}` を返す。

    `settings` は `config.Settings` 全体 (market_tools.build と同じ受け
    渡し方 — 内部で `settings.plugin` だけを sandbox 層に渡す)。

    labels.json 欠落・parse 不能・スキーマ不正 (トップレベルが dict で
    ない・"bars"/"expected" キー欠落や型不正・bars 各行が
    [iso,o,h,l,c,v] の 6 要素でない/非数値・bars の時刻が昇順でない・
    expected 各要素のキー過不足・direction が long/short 以外・expected
    が空リスト)・`meta.kind != "signal"` はすべて `ValueError`
    (fail closed)。`SandboxError` (plugin 実行時の timeout・クラッシュ・
    戻り値スキーマ不正) は catch せずそのまま伝播させる (モジュール
    docstring の fail closed 節を参照)。
    """
    if meta.kind != "signal":
        raise ValueError(
            f"plugin {meta.name!r}: evaluate_detection requires "
            f"meta.kind == 'signal', got {meta.kind!r}")

    labels = _load_labels(meta)
    df = _bars_to_df(labels["bars"])
    expected = _parse_expected(labels["expected"])

    predicted: PredictedSet = set()
    if sandbox_run is None:
        with plugin_sandbox.PluginSession(meta, settings=settings.plugin) as session:
            for bar_ts, signals in _walk_forward(meta, df, session.call):
                _collect(predicted, bar_ts, signals)
    else:
        def _call(payload: dict[str, Any]) -> dict[str, Any]:
            return sandbox_run(meta, payload, settings=settings.plugin)

        for bar_ts, signals in _walk_forward(meta, df, _call):
            _collect(predicted, bar_ts, signals)

    return _score(predicted, expected)


# --- ウォークフォワード ----------------------------------------------------

def _walk_forward(meta: PluginMeta, df: pd.DataFrame,
                  call: Callable[[dict[str, Any]], dict[str, Any]],
                  ) -> Iterator[tuple[pd.Timestamp, list[dict[str, Any]]]]:
    """`df` の各バー時点 `i` について `df.iloc[:i+1].tail(meta.max_bars)`
    を payload の `df` として `call()` を 1 回呼び、`(df.index[i],
    signals)` を yield する。warmup 不足時に空リストを返すのは plugin
    自身の責務 (サンプル plugin の作者向け注意と同じ前提) — ここでは
    `i=0` から素直に呼ぶ。"""
    for i in range(len(df)):
        window = df.iloc[:i + 1].tail(meta.max_bars)
        payload = {"df": window, "params": meta.params}
        result = call(payload)
        yield df.index[i], result["signals"]


def _collect(predicted: PredictedSet, bar_ts: pd.Timestamp,
            signals: list[dict[str, Any]]) -> None:
    for sig in signals:
        predicted.add((bar_ts, sig["direction"]))


def _score(predicted: PredictedSet, expected: PredictedSet) -> dict[str, Any]:
    tp = len(predicted & expected)
    fp = len(predicted - expected)
    fn = len(expected - predicted)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return {"precision": precision, "recall": recall, "tp": tp, "fp": fp, "fn": fn,
            "n_labels": len(expected)}


# --- labels.json 読み込み + スキーマ検証 (fail closed) --------------------

def _load_labels(meta: PluginMeta) -> dict[str, Any]:
    labels_path = meta.path / "labels.json"
    if not labels_path.is_file():
        raise ValueError(
            f"plugin {meta.name!r}: labels.json not found at {labels_path}")
    try:
        raw = json.loads(labels_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise ValueError(
            f"plugin {meta.name!r}: labels.json is not valid JSON ({exc})") from exc
    if not isinstance(raw, dict):
        raise ValueError(
            f"plugin {meta.name!r}: labels.json top-level must be an object")
    if "bars" not in raw:
        raise ValueError(f"plugin {meta.name!r}: labels.json missing 'bars' key")
    if "expected" not in raw:
        raise ValueError(f"plugin {meta.name!r}: labels.json missing 'expected' key")
    if not isinstance(raw["bars"], list):
        raise ValueError(f"plugin {meta.name!r}: labels.json 'bars' must be a list")
    if not isinstance(raw["expected"], list):
        raise ValueError(f"plugin {meta.name!r}: labels.json 'expected' must be a list")
    return raw


def _parse_utc_timestamp(value: Any, field: str) -> pd.Timestamp:
    if not isinstance(value, str):
        raise ValueError(f"labels.json {field} must be a str, got {value!r}")
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"labels.json {field} is not a valid timestamp: {value!r}") from exc
    if ts.tzinfo is None:
        return ts.tz_localize("UTC")
    return ts.tz_convert("UTC")


def _bars_to_df(bars_raw: list) -> pd.DataFrame:
    if not bars_raw:
        raise ValueError("labels.json 'bars' must be a non-empty list")

    index: list[pd.Timestamp] = []
    rows: list[list[float]] = []
    prev_ts: pd.Timestamp | None = None
    for i, row in enumerate(bars_raw):
        if not isinstance(row, list) or len(row) != 6:
            raise ValueError(
                f"labels.json bars[{i}] must be [iso, o, h, l, c, v] "
                f"(6 elements), got {row!r}")
        iso, o, h, l, c, v = row
        ts = _parse_utc_timestamp(iso, f"bars[{i}][0]")
        values: list[float] = []
        for j, val in enumerate((o, h, l, c, v)):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(
                    f"labels.json bars[{i}][{j + 1}] must be a number, got {val!r}")
            values.append(float(val))
        # 昇順前提のウォークフォワードを崩す入力は fail closed で拒否する
        # (順序が崩れると bar_ts の対応がずれ評価結果が無意味になる)。
        if prev_ts is not None and ts <= prev_ts:
            raise ValueError(
                f"labels.json bars[{i}]: timestamps must be strictly ascending "
                f"({ts} is not after {prev_ts})")
        prev_ts = ts
        index.append(ts)
        rows.append(values)

    return pd.DataFrame(rows, columns=_OHLCV_COLUMNS, index=pd.DatetimeIndex(index))


def _parse_expected(expected_raw: list) -> PredictedSet:
    if not expected_raw:
        raise ValueError("labels.json 'expected' must be a non-empty list")

    out: PredictedSet = set()
    for i, item in enumerate(expected_raw):
        if not isinstance(item, dict):
            raise ValueError(f"labels.json expected[{i}] must be an object")
        unknown = set(item) - _EXPECTED_ALLOWED_KEYS
        if unknown:
            raise ValueError(
                f"labels.json expected[{i}] has unknown keys: {sorted(unknown)}")
        missing = _EXPECTED_ALLOWED_KEYS - set(item)
        if missing:
            raise ValueError(
                f"labels.json expected[{i}] missing keys: {sorted(missing)}")

        ts = _parse_utc_timestamp(item["bar_ts"], f"expected[{i}].bar_ts")
        direction = item["direction"]
        if direction not in _VALID_DIRECTIONS:
            raise ValueError(
                f"labels.json expected[{i}].direction must be 'long' or "
                f"'short', got {direction!r}")
        out.add((ts, direction))
    return out
