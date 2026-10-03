---
id: candidate-strategy-backtest-runs-without-landlock
title: 候補 strategy の backtest が Landlock の無い plugin worker で動く
status: 裁定待ち
priority: 高
opened: 2026-10-03
closed: null
related: []
---
# [candidate-strategy-backtest-runs-without-landlock] 候補 strategy の backtest が Landlock の無い plugin worker で動く

**状態**: 裁定待ち / **優先**: 高

## 現象

改善 mission の run_backtest は候補 (未承認) の strategy コードを PluginSession で実行する (improve_loop.py:1055-1142 → strategy_adapter → sandbox.PluginSession)。PluginSession には Landlock が無く (rlimit、最小 env、AST 検査、socket module の差し替えのみ)、実測で data/agentic.db を書き込みで open でき、鍵ファイルも読める (tmp/design-ops-api/measure/landlock-socket.md)。操作 API の認証はこの worker に対して防御にならない。

## 原因

## 処置案・裁定

plugin worker にも gate pytest と同じ Landlock (読み取り allowlist、data/ と ~/.config を外す) を掛ける。live の signal 計算 worker も同じ。所要時間と互換性 (numpy 等の .so) は gate 側の実績で見積もる。操作 API の前提条件にはしない (2026-10-03 裁定)。

## 修正内容

## 経緯

- 2026-10-03: 起票。
- 2026-10-03: 状態: 裁定待ち — 2026-10-03 裁定: 操作 API の前提条件として先に実施する (設計レビュー r1 で、Landlock の無い plugin worker が鍵を読めるため承認・資金保護の解除を API に載せられないと判定)。設計 C0 を tmp/design-plugin-landlock/ で作成中。
