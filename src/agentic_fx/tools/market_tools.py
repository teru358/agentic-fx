"""market 系ツール — datafeed の薄い読み取り専用ラッパー。"""
from __future__ import annotations

from agentic_fx.config import Settings
from agentic_fx.datafeed.bars import bars_to_df
from agentic_fx.datafeed.econ_calendar import EconCalendar
from agentic_fx.datafeed.indicators import compute_indicators
from agentic_fx.datafeed.price_provider import PriceProvider
from agentic_fx.tools.registry import ToolDef


def _pair_param(settings: Settings) -> dict:
    """timeframe の enum は設定から動的に作る (足を固定しない方針)。"""
    return {"pair": {"type": "string", "description": "e.g. USDJPY"},
            "timeframe": {"type": "string",
                          "enum": list(settings.datafeed.intervals)}}


def build(provider: PriceProvider, econ: EconCalendar,
          settings: Settings) -> list[ToolDef]:
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
        """
        return compute_indicators(_frame(pair, timeframe))

    def get_econ_calendar(days: int = 1) -> list[dict]:
        return econ.upcoming(hours=days * 24)

    pair_param = _pair_param(settings)
    return [
        ToolDef("get_ohlcv", "OHLCV 価格データ (直近 100 本)",
                {"type": "object", "properties": pair_param,
                 "required": ["pair", "timeframe"]}, get_ohlcv),
        ToolDef("get_indicators",
                "テクニカル指標 (SMA/EMA/RSI/ATR/MACD/BB)。"
                "上位足を見たい場合は timeframe を変えて呼び直す",
                {"type": "object", "properties": pair_param,
                 "required": ["pair", "timeframe"]}, get_indicators),
        ToolDef("get_econ_calendar", "経済指標カレンダー (今後 N 日)",
                {"type": "object",
                 "properties": {"days": {"type": "integer", "minimum": 1,
                                         "maximum": 7}},
                 "required": []}, get_econ_calendar),
    ]
