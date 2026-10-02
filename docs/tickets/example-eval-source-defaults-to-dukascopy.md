---
id: example-eval-source-defaults-to-dukascopy
title: 設定例の履歴源の既定が dukascopy で大量取得を促す
status: 設計待ち
priority: 中
opened: 2026-10-02
closed: null
related: []
---
# [example-eval-source-defaults-to-dukascopy] 設定例の履歴源の既定が dukascopy で大量取得を促す

**状態**: 設計待ち / **優先**: 中

## 現象

settings.yaml.example の backtest.eval_source が dukascopy。importer は 1 秒間隔で最大 500 リクエスト/回、レート制限の設計が無い。作者の IP は遮断の前歴あり (2026-10-02 の 1 件実測は 200・16.9 秒)。

## 原因

## 処置案・裁定

2026-10-02 裁定: bridge を使わない場合は履歴源なし。「履歴源なし」を表す値を設け、example の既定を見直す。初期設定ウィザードの設計で決める。

## 修正内容

## 経緯

- 2026-10-02: 起票。
