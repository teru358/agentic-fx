---
id: legacy-t13-f9
status: 設計待ち
priority: 未設定
opened: 2026-08-30
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-t13-f9]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[T13-F9]/[T13-F9 改訂] codex realbackend プローブ失敗: 真因 = codex 0.150.x が `model_providers.<id>.name` を必須化 (2026-08-30、`codex_runner._build_argv` へ name 追加で是正済み)。auth.json 因果は却下済み (verified-codex-round1 束 F)。**残**: c2 失敗が provider=chatgpt だった場合は別因の可能性 — 手順 1 (chatgpt) 実施時に確認。settings.yaml.example の codex.bin プレースホルダ解消待ち

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

