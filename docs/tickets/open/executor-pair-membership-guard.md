---
id: executor-pair-membership-guard
title: 発注経路に Order.pair ∈ settings.pairs の決定論的検査が無い
status: 設計待ち
priority: 高
opened: 2026-10-25
closed: null
related: [reference-symbols-as-indicator-inputs]
kind: 不具合
---
# [executor-pair-membership-guard] 発注経路に Order.pair ∈ settings.pairs の決定論的検査が無い

**状態**: 設計待ち / **優先**: 高 / **種別**: 不具合

## 現象

参照系列 C0 の設計レビュー (sol、2026-10-25) で判明: 発注対象の限定は trade_intent_schema の enum (LLM 出力の誘導) だけで、TradeIntent.from_llm_dict は非空文字列しか確認せず、Executor.record_and_validate_intent は origin / mission だけ、_open と gather_open_snapshot は membership 検査前に quote_fn(intent.pair) を呼び、risk gate は pair_rules の有無を見るだけ (settings.pairs 外の余剰 key を禁じていない)。provider / spec / risk 設定が揃った環境では settings.pairs 外の symbol が quote / spec / broker に到達し得る。処置案: executor の共有入口 (通常の _open と五相経路の gather_open_snapshot / open_from_snapshot の両方) で、外部 quote / spec 取得より前に `action=open && intent.pair not in settings.pairs → rejected / not_tradable_symbol` を決定論的に検査し、quote / spec / broker 呼出し 0 回を test で pin。pair_rules の余剰 key は起動時に拒否。参照系列 (reference-symbols-as-indicator-inputs) の前提条件。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与
