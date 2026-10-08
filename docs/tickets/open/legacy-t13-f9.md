---
id: legacy-t13-f9
title: codex プローブ失敗の残件 (chatgpt 経路の確認と bin 設定)
status: 設計待ち
priority: 低
opened: 2026-08-30
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-t13-f9]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[T13-F9]/[T13-F9 改訂] codex realbackend プローブ失敗: 真因 = codex 0.150.x が `model_providers.<id>.name` を必須化 (2026-08-30、`codex_runner._build_argv` へ name 追加で是正済み)。auth.json 因果は却下済み (verified-codex-round1 束 F)。**残**: c2 失敗が provider=chatgpt だった場合は別因の可能性 — 手順 1 (chatgpt) 実施時に確認。settings.yaml.example の codex.bin プレースホルダ解消待ち

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: codex プローブ失敗の残件 (chatgpt 経路の確認と bin 設定) — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): 真因は是正済み、chatgpt 経路での再確認と example のプレースホルダが残る。 根拠: 真因 (provider name 必須化) は codex_runner で是正済み。残りは provider=chatgpt 時の c2 失敗の切り分けと settings.yaml.example:32 の codex.bin プレースホルダで、実機確認が要る。
