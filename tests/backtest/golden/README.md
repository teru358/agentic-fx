# Backtest golden-v1

`golden-v1.json` は固定した USDJPY の合成 1 分足（三日間）、固定 settings、
固定 start/end、in-process の `IntentSource`、fresh in-memory DB から作る
段階 1 の観測基線です。plugin subprocess は使いません。

再生成は worktree で次を実行します。

```bash
PYTHONPATH=src VIRTUAL_ENV=/home/teru358/project/agentic-fx/.venv \
UV_CACHE_DIR=/tmp/claude-1000/uv-bis1 \
uv run --active --no-sync python -m tests.backtest.golden.generate
```

v1 は現行の先頭足規則をそのまま記録します。段階 3 で先頭足規則を変更した際は、
その変更を説明したうえで v2 として生成・置換します。fixture は SL、TP、及び
kill switch のラッチを含むため、空の kill-switch 観測面は golden 不一致になります。
