# Backtest golden-v2

`golden-v1.json` は段階 2 の基線として残す。`golden-v2.json` は固定した
USDJPY の合成 1 分足（三日間）、固定 settings、
固定 start/end、in-process の `IntentSource`、fresh in-memory DB から作る
段階 1 の観測基線です。plugin subprocess は使いません。

再生成は worktree で次を実行します。

```bash
PYTHONPATH=src VIRTUAL_ENV=/home/teru358/project/agentic-fx/.venv \
UV_CACHE_DIR=/tmp/claude-1000/uv-bis1 \
PYTHONPATH=src uv run --no-sync python -m tests.backtest.golden.generate
```

v1 は現行の先頭足規則をそのまま記録します。段階 3 で先頭足規則を変更した際は、
その変更を説明したうえで v2 として生成・置換します。fixture は SL、TP、及び
kill switch のラッチを含むため、空の kill-switch 観測面は golden 不一致になります。

| 差分分類 | 許容対象 |
| --- | --- |
| pre-decision | `first_decision_at(v2)` より前の snapshots / kill-switch event |
| derived | その期間の signal に由来する order と、その order に連なる equity 点 |

上記に機械分類できない差分は失敗です。機械分類は
`tests/backtest/test_golden_diff_v1_v2.py` が実行し、件数を固定値として
pin する (ずれたら本表も更新すること)。

## golden-v1 → golden-v2 差分分類 (機械検証済み・段階 3)

| 分類 | 件数 |
| --- | --- |
| pre-decision snapshots | 60 |
| pre-decision equity_curve | 59 |
| pre-decision kill_switch_events | 0 |
| derived orders | 0 (このシナリオでは全シグナルが first_decision_at(v2) 以降由来のため該当なし) |
| 分類不能 | 0 |

段階 3 の先頭足規則 (A2) により、golden-v2 の `first_decision_at` は v1 の
`start` (12:00) から `13:00` へ後ろ倒しになった。差の実体は「12:00〜12:59
の warmup 中、v2 では Scheduler が動かないため snapshots/equity_curve の
観測記録が作られない」ことのみで、orders・metrics は逐語一致した。

## golden-v2 → golden-v3 差分分類

v2 は保持する。v3 は replay kill-switch 評価規約を正とし、既存 fixture
期間内には次の市場再開がないため auto-release と snapshot 追加はない。

| 分類 | 件数 |
| --- | ---: |
| added_metric_keys | 1 |
| removed_duplicate_activity_events | 1 |
| changed_existing_values | 0 |
| unexpected_added_rows | 0 |
| unclassified | 0 |

`tests/backtest/test_golden_diff_v2_v3.py` が上表と、6 種の不許可差分を
機械検証する。分類不能が 1 件でもあれば golden は更新しない。
