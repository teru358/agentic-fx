"""配備済み indicator の縮小 corpus (名前と先頭 docstring の行だけの写し、式は持たない)。

銘柄名を含む 4 本 (名前検査に掛かる) と、掛からない代表 6 本。`materialize` が
tmp 配下に plugin.py / config.yaml / test_plugin.py を書く。"""
from __future__ import annotations

from pathlib import Path

import yaml

SYMBOL_NAMED: dict[str, tuple[str, str]] = {
    "us500_hourly_drift": (
        "US500 UTC-hour conditional drift estimate from prior same-hour returns.",
        "hourly_drift"),
    "usoil_utc_day_open_return": (
        "USOIL UTC日初1時間バーの方向リターンを日中に保持する指標。",
        "utc_day_open_return"),
    "xau_hourly_volatility_surprise": (
        "UTC時刻ごとの金価格リターン変動を比較する指標。",
        "hourly_volatility_surprise"),
    "xau_utc_open_gap_reversal": (
        "XAUUSD UTC 日初バーのギャップ反転を示す指標。",
        "utc_open_gap_reversal"),
}

CLEAN: dict[str, tuple[str, str]] = {
    "volume_per_bar": ("Volume per bar indicator.", "volume_per_bar"),
    "copper_x": ("Copper-like ratio indicator.", "copper_x"),
    "atr": ("ATR (Average True Range) indicator plugin.", "atr"),
    "rsi": ("RSI (Relative Strength Index) indicator plugin.", "rsi"),
    "bollinger": ("Bollinger bands indicator plugin.", "middle"),
    "tick_volume_adjusted_move": (
        "Tick-volume-adjusted absolute return indicator.", "tick_volume_adjusted_move"),
}

CORPUS: dict[str, tuple[str, str]] = {**SYMBOL_NAMED, **CLEAN}


def materialize(root: Path) -> list[Path]:
    """各 indicator のディレクトリを root 直下に書き、そのパスを返す。"""
    paths = []
    for name, (doc, output) in CORPUS.items():
        directory = root / name
        directory.mkdir(parents=True)
        (directory / "plugin.py").write_text(
            f'"""{doc}"""\n\n\ndef compute(df, params):\n    return {{"{output}": df["close"]}}\n',
            encoding="utf-8")
        (directory / "config.yaml").write_text(yaml.safe_dump(
            {"kind": "indicator", "outputs": [output], "max_bars": 400, "params": {}},
            sort_keys=False), encoding="utf-8")
        (directory / "test_plugin.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
        paths.append(directory)
    return paths
