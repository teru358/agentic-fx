---
id: datafeed-no-tick-minutes-counted-as-gap
title: 週明けの tick 無し時間を欠損と数え 15m 取得が約 1 日失敗
status: 設計待ち
priority: 低
kind: 不具合
opened: 2026-11-02
closed: null
related: []
---
# [datafeed-no-tick-minutes-counted-as-gap] 週明けの tick 無し時間を欠損と数え 15m 取得が約 1 日失敗

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

週明け開場 (日曜 21:00 UTC) 直後に MT5 USDJPY の tick が薄く、1m 足の無い時間 (2026-11-01 は 21:05〜22:00 の 56 分) を datafeed/health.py:184 の健全性検査が「開場中の欠損」と数え、15m の OHLCV / indicators が DataUnhealthy (gap of N open-market bars) になる。検査窓から欠損が抜けるまで続き、取引判断 LLM は判断足を見られず hold (11/01 開場以降 46 件中 23 件)。毎週再発 (開場後 1 時間の 1m 足: 10/18 60 本、10/25 36 本、11/01 4 本)。発注誤りは無く機会損失のみ、資金保護経路は無関係。処置案 (推奨): 真の取得障害の正本 datafeed_outage_gap を使い、障害区間に重ならない tick 無し時間は欠損に数えない (econ 設計 §3.5 の欠損 2 分類と同じ考え)。簡易案 (週の開場後 N 分だけ許容) は平日深夜の tick 無しを救えないので採らない

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-02: 起票。
- 2026-11-03: 原因の注記。2026-11-02 の hold 多発の主因は tick 無しではなく、冬の日曜 21:00〜22:00 UTC を開場と誤判定していたこと (market-hours-fixed-21utc-ignores-dst)。残る論点は read tool の欠損注記の設計 (2026-11-02-read-tool-gap-annotate-design、未 commit) で、DST 是正の後に v0.2。
- 2026-11-03: 状態: 設計待ち、優先: 低 — 2026-11-03: ユーザー裁定で保留。DST 是正後の実測で 15m / 1h の gap は 0、残るのは 1m / 5m の 27 日で 7 区間 (日曜開場 5、rollover 1、平日 1)。設計 v0.2 は docs/superpowers/specs/2026-11-02-read-tool-gap-annotate-design.md (r1 指摘反映済み、実装は econ 束と T4 の後に再判断)
