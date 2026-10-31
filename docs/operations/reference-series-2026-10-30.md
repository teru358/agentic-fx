# 参照系列 (株価指数・原油などの参照銘柄) の使い方 (2026-10-30)

MT5 bridge が配信する CFD を、**発注しない分析用の参照銘柄**として取り込み、相関分析・読み取り tool・strategy の indicator 入力に使うための運用手順。設計の正は [参照系列の設計](../superpowers/specs/2026-10-25-reference-series-design.md)。新しい CLI は無い。既存の `afx history` / `afx analyze` / `afx check` / `afx init` をそのまま使う。

## 1. 参照銘柄を決める

- 設定は `datafeed.watch_symbols` (MT5 broker の raw 名、1 つ 32 bytes まで、`analysis.max_watch_symbols` 件まで)。取引 pair (`pairs`) と重ねない。空でないときは `datafeed.primary: mt5` が必須 (yfinance / Twelve Data のままでは起動を拒む)。
- `afx init` の段 3 で、MT5 bridge に接続できたときだけ、bridge の銘柄一覧 (`GET /symbols`) から選べる。初回は何も選ばなくてよい (既定は空)。再実行の既定は現在の値で、`-` で全部外す。一覧を取れない (接続・認証・時間切れ) ときは理由を示して質問を飛ばし、現在の値は変えない。段 9 の要約に出て、段 10 で書き込むまで何も書かない。yfinance / Twelve Data を選んだときは尋ねない。
- 手で書くときは `config/settings.yaml` の `datafeed.watch_symbols` に raw 名を並べる (例 `[JP225, US500]`)。schema v4 から上げるときに取引 pair と重なる値・重複があると移行が止まる。自動では消さないので、片方を消してから `afx init` を再実行する。

## 2. 検査 (`afx check`)

`afx check` と daemon の起動前検査は、`watch_symbols` が空でなく価格源が MT5 のときだけ bridge の `/symbols` と突き合わせる。

| 表示 | 意味 |
|---|---|
| `warning: watch_symbol_unknown` | 一覧に無い名前がある (綴りか broker の suffix を確かめる)。起動は止めず、その銘柄の取得は失敗する |
| `warning: watch_symbols_unchecked` | bridge に届かない・認証が通らない・応答が想定外で、確かめていない。unknown とは言わない |

`afx check` は `.env` の `MT5_BRIDGE_API_KEY` も読む。参照銘柄が空のときは bridge を呼ばず、出力は従来と同じ。

## 3. 履歴の取り込みと分析

参照銘柄の履歴は取引 pair と同じ `source=mt5`・同じ base interval で取り込む。

```
afx history import --source mt5 --symbol JP225 --from 2026-09-01 --to 2026-10-01 --interval 1m
afx history coverage --symbol JP225 --timeframe 1h --source mt5 --from 2026-09-01 --to 2026-10-01
afx analyze corr --a USDJPY --b JP225 --timeframe 1h --source mt5
```

- `history import --symbol` は raw 名をそのまま bridge に渡す。`history coverage` は 1 回に 1 銘柄だけを表示し、参照銘柄の一覧表は無い。
- `analyze corr` は両方の銘柄が同じ `--source` の 1m 履歴を持ち、共通の観測が 30 本以上あるときだけ通る。live で 1h の足を取っているだけでは使えない。
- 参照銘柄の足は live では `ohlcv_cache` に `mt5-live` として保存され、`ohlcv_history` は import 専用である。

## 4. 読み取り tool

取引判断・改善の mission の `get_ohlcv` / `get_indicators` は、`pairs` に加えて `watch_symbols` も受ける (読み取りだけ。bridge の raw 名で取得する)。`get_signals` と取引意図の `pair` は取引 pair だけのままで、参照銘柄は発注できない。tool は呼出しのたびに現在の設定の集合で再検査する。

## 5. 反映

`watch_symbols` を変えたら daemon を再起動する (`systemctl --user restart afx`)。settings schema v4 の設定は起動時の移行で v5 に上がる。実機での手順はリリースの確認事項を参照する。
