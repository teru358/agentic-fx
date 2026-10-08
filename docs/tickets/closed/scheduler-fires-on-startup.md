---
id: scheduler-fires-on-startup
title: 起動直後の trade 発火と空振り backtest の問題は是正済
status: 是正済
priority: 未設定
opened: null
closed: 2026-10-02
related: []
backfilled: true
source_section: 未完了
---
# [scheduler-fires-on-startup]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[scheduler-fires-on-startup] (観測、run8 C) trade mission は毎時 hh:12 だけでなく afx 起動直後にも発火する — E2E の時間帯回避は「起動 → trade 完走待ち → 種まき」の順が必要 `_is_successful_backtest` は metrics の中身を見ないため trades=0 / evaluable=false の空振り backtest 1 本で Tier B が恒久解除され、モデルが self-test 修正ループへ復帰する。directive の文言「実データで trade が出れば plugin は正しい」と不整合。是正案: 成功 = `evaluable` かつ trades ≥ 1 (設計 v4 Tier B の「成功 backtest」定義を明確化して再レビュー)

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。cron_cursor 永続と前セッション足の skip で起動直後発火を抑止、成功 backtest は trades>0 に。 根拠: b0de51f (session_start と前セッション足の skip、cron_cursor の永続)。Tier B の空振り backtest は 6f01221 で是正。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: 起動直後の trade 発火と空振り backtest の問題は是正済 — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。cron_cursor 永続と前セッション足の skip で起動直後発火を抑止、成功 backtest は trades>0 に。 根拠: b0de51f (session_start と前セッション足の skip、cron_cursor の永続)。Tier B の空振り backtest は 6f01221 で是正。
