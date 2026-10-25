---
id: news-default-sources-to-user-input
title: 同梱の既定ニュース源を廃止し afx init の入力式にする
status: 実装中
priority: 中
opened: 2026-10-25
closed: null
related: [news-source-cli-and-429-backoff]
---
# [news-default-sources-to-user-input] 同梱の既定ニュース源を廃止し afx init の入力式にする

**状態**: 実装中 / **優先**: 中 / **種別**: 機能

## 現象

src/agentic_fx/datafeed/default_sources.py の DEFAULT_SOURCES 3 本 (yahoo-finance-topstories / nhk-keizai / fxstreet-news、うち 2 本は商用サイト) が afx init と起動時の seed_default_sources で自動登録される。これは利用者責任の原則 (源は利用者の判断、規約確認は利用者) と合わない。yahoo-finance-topstories は 2026-09-02 以降停止している。裁定 (2026-10-25 ユーザー): 同梱をやめ、afx init で名前と URL を入力式にする (空 Enter で飛ばす、後から afx news add)。候補例は README と受入手順書に注記付きで載せる (yahoo は除く)。既存 DB の既登録 3 源は消さない (利用者が afx news disable / 将来の remove で扱う)。afx news 設計 (2026-10-25-news-source-cli-design.md) の T2 に統合する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
- 2026-10-25: 状態: 実装中 — 2026-10-25: T1 / T2a / T2b を main に merge (81461a61)。残り AC-1 の本機確認 (daemon 再起動後に yahoo を disable、次の収集周期で backoff と status を観測)
