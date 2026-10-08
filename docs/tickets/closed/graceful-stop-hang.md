---
id: graceful-stop-hang
status: 是正済
priority: 未設定
opened: 2026-09-27
closed: 2026-09-27
related: [graceful-stop-waits-mission]
backfilled: true
source_section: 是正済み
---
# [graceful-stop-hang]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[graceful-stop-hang] **閉 2026-09-27** — 停止 461 秒 (9/27 08:39、b0de51f 以前の supervisor join) は B-2 段 a の supervisor.shutdown 改修で解消。段 c 後 1 秒未満、段 d 後 2 秒の 2 回連続で確認 (`tmp/impl-b1/RESUME.md`)。[graceful-stop-waits-mission] (走行中 improve mission の timeout 待ち、低) は別件で open のまま

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

