---
id: pine-script-to-plugin
title: Pine script を plugin に変換する手段が無い
status: 設計待ち
priority: 低
opened: 2026-09-12
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [pine-script-to-plugin]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[pine-script-to-plugin] (設計待ち、ユーザー要望 2026-09-12) TradingView Pine script を agentic-fx plugin (indicator/strategy) に変換する仕組み。Pine の意味論 (series / security / strategy.* 発注) → plugin 契約 (`compute` / `evaluate`、config.yaml 許可キー) の写像と、変換結果の検証方法 (Pine 側の出力との突合データが要る) が設計課題。改善ループ像 (指標層 → 戦略層) の指標供給経路の 1 つ

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: Pine script を plugin に変換する手段が無い — 仕分け (2026-10-02、現物で成立を確認): 意味論の写像と突合検証方法が設計課題、指標供給経路の 1 つ。 根拠: Pine script 変換の仕組みは src に無く commit も無い。
