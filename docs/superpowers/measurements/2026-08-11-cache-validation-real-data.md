# cache validation window 実データ実測 (2026-08-11)

- DB: measure-src.db (運用 DB `agentic.db` をコピーし `init_db` で `ohlcv` → `ohlcv_cache`/`ohlcv_history` へ分割移行したもの)
- symbol/source/interval: USDJPY / history-source=mt5, window-source=yfinance / 1m
- 期間: 2026-07-20T00:00:00+00:00 から 2026-08-11T23:59:00+00:00
- 48 時間以上の週末ギャップ数: 3
- 反復数: 7
- 実行コマンド:
  ```
  uv run python scripts/measure_cache_validation_window.py \
    --db /tmp/measure-src.db \
    --symbol USDJPY \
    --history-source mt5 \
    --window-source yfinance \
    --interval 1m \
    --lookback-days 5 \
    --repeats 7 \
    --output /tmp/cache-validation-real-data.json
  ```
- peak bytes は `validate_bars` 呼び出し中の**増分**割当 (バー列本体は計測開始前に確保済み)。§1.4 の 161.9 MB とは測定対象が異なるので大小比較しない

| 条件 | 行数 | validate_bars 合否 | 失敗理由 | 中央値秒 | peak bytes |
|---|---:|---|---|---:|---:|
| 窓縮小前 (全期間) | 24465 | PASS | なし | 0.1762111020507291 | 672 |
| 窓縮小後 (要求窓) | 4316 | PASS | なし | 0.029264859040267766 | 576 |

## 結論

窓縮小の前後で `validate_bars` の合否はどちらも PASS のまま変わらなかった。窓外のデータを検査対象から外したことで、行数は 24465 行から 4316 行へ (約 5.7 分の1)、中央値実行時間は 0.176211… 秒から 0.029265… 秒へ (約 6.0 倍の短縮) 減少した。peak bytes (増分割当) は 672 bytes から 576 bytes とわずかに減少した。これらの差は反復 7 回中央値であり run 間ばらつきに埋もれない大きさである。§1.4 の 180 日連続合成値 5.76 秒 / 161.9 MB とは測定対象・データ性質が異なるため比較・代用しない。
