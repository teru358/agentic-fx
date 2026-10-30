---
id: econ-event-actuals-and-reaction-stats
title: 指標の発表実績を取得し、種別ごとの価格反応を決定論的に集計する
status: 裁定待ち
priority: 中
kind: 機能
opened: 2026-10-30
closed: null
related: [econ-event-hold-window-deterministic, llm-vs-strategy-signal-authority]
---
# [econ-event-actuals-and-reaction-stats] 指標の発表実績を取得し、種別ごとの価格反応を決定論的に集計する

**状態**: 裁定待ち / **優先**: 中 / **種別**: 機能

## 現象

econ_events は forecast / previous だけで actual が 1026 件中 0 件 (econ_calendar.py は発表前の予定だけ取り込み、発表後に実績を更新しない)。発表内容と、それを受けた価格推移が蓄積されていない。対策: (1) 発表後に actual を更新 (surprise = actual − forecast)、(2) イベント種別 (国 × 名前) ごとに直近 N 回の発表後 15m / 1h / 4h の値動き幅と surprise の符号との関係を ohlcv_cache の 1m 足から決定論的に集計 (新しい保存でなく派生集計)、(3) 使い道は回避帯の幅を種別ごとに実測で決めることと、tool で LLM に数値統計を渡すこと。LLM に過去の物語を読ませる形は採らない (CPI 年 12 回 / FOMC 年 8 回で標本が小さく過剰一般化する)。設計前にブレスト (ユーザー要望) と sol の前提確認

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-30: 起票。
- 2026-10-30: 状態: 裁定待ち — ブレスト → sol 前提確認 → 設計 C0 の順 (3 件を一束で)
