# Improve Mission 2043 — gate failed

reason: `unprofitable`

No approval request was produced by this mission.

## Profitability floor detail

failed_stage: in_sample
failed_pairs: USDJPY, EURUSD
thresholds: min_pf=1.0 require_positive_avg_r=True require_holdout_evaluable=False max_drawdown=0.2 max_trades_per_eval_bar=0.25
### in_sample

| pair | trades | pf | win_rate | avg_r | max_drawdown | total_pnl | kill_switch_latches | evaluable | failed |
|---|---|---|---|---|---|---|---|---|---|
| USDJPY | 50 | 0.5 | 0.5 | -0.1 | 0.263 | 100.0 | 0 | True | True |
| EURUSD | 50 | 0.5 | 0.5 | -0.1 | 0.05 | 100.0 | 0 | True | True |

### floor_conditions

| pair | max_drawdown | eval_bars | trades_per_eval_bar | failed_kinds |
|---|---|---|---|---|
| USDJPY | 0.263 | 1000.000 | 0.050 | drawdown_too_deep |
| EURUSD | 0.050 | 1000.000 | 0.050 | - |

floor_failed: USDJPY drawdown_too_deep (max_drawdown=0.263 > 0.2)
