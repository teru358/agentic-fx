"""market 系ツール — datafeed の薄い読み取り専用ラッパー。"""
from __future__ import annotations

import logging

from agentic_fx.config import Settings
from agentic_fx.datafeed.bars import bars_to_df
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.indicators import compute_indicators
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.plugin import sandbox as plugin_sandbox
from agentic_fx.plugin.loader import PluginMeta
from agentic_fx.tools.registry import ToolDef

_log = logging.getLogger(__name__)


def pair_param(settings: Settings) -> dict:
    """pair と timeframe の enum は設定から動的に作る (足・通貨ペアを固定しない方針)。"""
    return {"pair": {"enum": list(settings.pairs), "description": "e.g. USDJPY"},
            "timeframe": {"type": "string",
                          "enum": list(settings.datafeed.intervals)}}


def build(provider: PriceProvider, econ: EconCalendar, settings: Settings, *,
          indicator_plugins: list[PluginMeta] | None = None,
          sandbox_run=None) -> list[ToolDef]:
    """`indicator_plugins` は `plugin_loader.approved_plugins()` の
    `result.inventory.metas` をそのまま渡してよい — `kind != "indicator"` の
    要素はここで無視する (kind="indicator" だけが `get_indicators` の対象)。

    [indicator-consumption-wiring] §2.3: 親 (`service.py`) と子
    (`mission_worker.py`) は**別々の composition root** で inventory を
    構築する。子は handshake の `plugins_dir` から再実行するため、
    live producer と版がずれ得る — LLM 向けの参考情報なので許容する。

    `sandbox_run` は既定 `None` で `plugin.sandbox.run_plugin` を使う。テストは
    fake を注入して実 subprocess を起動せずに合成ロジックだけを検証できる
    (実サンドボックス起動の検証は Task 2 のテストの関心)。
    """
    indicator_plugins = [m for m in (indicator_plugins or []) if m.kind == "indicator"]
    sandbox_run = sandbox_run if sandbox_run is not None else plugin_sandbox.run_plugin

    # 足の導出 (ネイティブ / resample) は provider の責務。ここでは
    # 要求された足をそのまま渡す — ツール層で resample すると、MT5 の
    # ネイティブ 4h が使える環境でも 1h から作り直してしまう
    def _frame(pair: str, timeframe: str):
        return bars_to_df(provider.get_bars(pair, timeframe))

    def get_ohlcv(pair: str, timeframe: str) -> list[dict]:
        df = _frame(pair, timeframe).tail(100)
        return [{"ts": ts.isoformat(), "open": r["open"], "high": r["high"],
                 "low": r["low"], "close": r["close"]}
                for ts, r in df.iterrows()]

    def get_indicators(pair: str, timeframe: str) -> dict:
        """要求された足の指標を返す。

        MTF は「別の足を要求して呼び直す」で表現する (旧実装は 1h のとき
        だけ mtf_4h を付けていたが、足を固定しない方針では 1h だけ特別扱い
        する根拠がない。どの足の上位足を見たいかは LLM / plugin が決める)。

        承認済み indicator plugin は、組み込み指標と**同じ `_frame(pair,
        timeframe)` の df** (要求された pair/timeframe の bars) を
        `meta.max_bars` で末尾クランプして渡す — 上位足を見たい plugin は
        signal/strategy 同様、自身の config.yaml で明示的に別 timeframe を
        持つのではなく、この get_indicators と同じ「呼び出し側が指定する
        timeframe」の枠組みに従う (indicator kind の config.yaml は
        timeframe を必須としない — 呼び出しごとに変わるため)。
        `plugin:<name>` キーで合成し、SandboxError (timeout・クラッシュ・
        スキーマ不正すべてを含む単一の例外) はそのキーだけを落として警告
        ログを出す — 組み込み指標は plugin の失敗に関わらず必ず返す
        (fail-open: plugin 出力は LLM 向けの参考情報であり、1 つの plugin
        の不調で get_indicators 全体を失敗させない)。
        """
        df = _frame(pair, timeframe)
        result = compute_indicators(df)
        for meta in indicator_plugins:
            if meta.max_bars > settings.plugin.max_bars_limit:
                _log.warning(
                    "plugin %s: max_bars %d exceeds settings.plugin.max_bars_limit "
                    "%d — skipping", meta.name, meta.max_bars,
                    settings.plugin.max_bars_limit)
                continue
            payload = {"df": df.tail(meta.max_bars), "params": meta.params}
            try:
                plugin_result = sandbox_run(meta, payload, settings=settings.plugin)
            except plugin_sandbox.SandboxError as exc:
                _log.warning("plugin %s: indicator failed (%s) — dropping "
                             "plugin:%s key (built-ins unaffected)",
                             meta.name, exc, meta.name)
                continue
            result[f"plugin:{meta.name}"] = _project_indicator_output(plugin_result)
        return result

    def get_econ_calendar(days: int = 1) -> list[dict]:
        return econ.upcoming(hours=days * 24)

    pair_schema = pair_param(settings)
    return [
        ToolDef("get_ohlcv", "OHLCV 価格データ (直近 100 本)",
                {"type": "object", "properties": pair_schema,
                 "required": ["pair", "timeframe"]}, get_ohlcv),
        ToolDef("get_indicators",
                "テクニカル指標 (SMA/EMA/RSI/ATR/MACD/BB)。"
                "上位足を見たい場合は timeframe を変えて呼び直す。"
                "承認済み plugin の指標が併記される場合は `plugin:<name>` "
                "キーで区別できる",
                {"type": "object", "properties": pair_schema,
                 "required": ["pair", "timeframe"]}, get_indicators),
        ToolDef("get_econ_calendar", "経済指標カレンダー (今後 N 日)",
                {"type": "object",
                 "properties": {"days": {"type": "integer", "minimum": 1,
                                         "maximum": 7}},
                 "required": []}, get_econ_calendar),
    ]


def _project_indicator_output(plugin_result: dict) -> dict:
    """[indicator-consumption-wiring] §2.5: 系列は**末尾値**へ射影し、

    **入力は `sandbox.run_plugin` の戻り (= `_validate_indicator_result` を
    通した後) に限る** — 系列は `list[float | None]`、スカラー NaN は
    `None` (opus r1 I4 で契約を固定)。wire 封筒 `{"series": [...]}` は
    ここへは来ない。

    値が未確定 (スカラー NaN = None、系列末尾 None) のキーは落とす
    (fail-open 維持 — LLM 向けの参考情報なので「値が無い」ことを
    `null` で見せるより落とす方が誤読が少ない)。空系列も落とす。"""
    out: dict = {}
    for key, value in plugin_result.items():
        if isinstance(value, list):
            if not value or value[-1] is None:
                continue
            out[key] = float(value[-1])
            continue
        if value is None:
            continue
        out[key] = value
    return out
