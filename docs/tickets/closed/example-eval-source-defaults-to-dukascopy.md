---
id: example-eval-source-defaults-to-dukascopy
title: 設定例の履歴源の既定が dukascopy で大量取得を促す
status: 是正済
priority: 中
opened: 2026-10-02
closed: 2026-10-16
related: []
---
# [example-eval-source-defaults-to-dukascopy] 設定例の履歴源の既定が dukascopy で大量取得を促す

**状態**: 是正済 / **優先**: 中

## 現象

settings.yaml.example の backtest.eval_source が dukascopy。importer は 1 秒間隔で最大 500 リクエスト/回、レート制限の設計が無い。作者の IP は遮断の前歴あり (2026-10-02 の 1 件実測は 200・16.9 秒)。

## 原因

## 処置案・裁定

2026-10-02 裁定: bridge を使わない場合は履歴源なし。「履歴源なし」を表す値を設け、example の既定を見直す。初期設定ウィザードの設計で決める。

## 修正内容

- 2026-10-16: first-run T13 で backtest.eval_source の example 既定を none に (model 既定も none)。MT5 を使う分岐はウィザードが mt5 を書く。main 2733093

## 経緯

- 2026-10-02: 起票。
- 2026-10-16: 是正内容 — first-run T13 で backtest.eval_source の example 既定を none に (model 既定も none)。MT5 を使う分岐はウィザードが mt5 を書く。main 2733093
