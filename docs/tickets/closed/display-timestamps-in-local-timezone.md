---
id: display-timestamps-in-local-timezone
title: 人が読む面の時刻を現地 tz で表示し、bridge.log も UTC に揃える
status: 是正済
priority: 中
kind: 機能
opened: 2026-11-02
closed: 2026-11-07
related: []
---
# [display-timestamps-in-local-timezone] 人が読む面の時刻を現地 tz で表示し、bridge.log も UTC に揃える

**状態**: 是正済 / **優先**: 中 / **種別**: 機能

## 現象

activity.log / agentic.log は UTC、mt5_bridge/logs/bridge.log は JST、afx ctl log / web / Discord は UTC 表示で、人が読むときに分かりづらい (2026-11-02 ユーザー指摘)。処置: 保存・ファイルは UTC のまま offset 明示、bridge.log も UTC に。表示面 (afx ctl log / activity / status、web、Discord) は設定 display.timezone (既定はシステム tz) で換算し tz 名を付ける

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-07: 2026-11-07 配備 (main eca50b88)。人が見る日時を display_timezone で換算し、表記は YYYY-MM-DD HH:MM:SS ±HH:MM (UTC も +00:00)、zone 名は status / web の見出しだけ。afx-ops は同梱 tzdata で隔離下でも換算、bridge のログは UTC 表記。本機で ctl status の見出し Asia/Tokyo (+09:00) と bridge.log の UTC 表記を確認

## 経緯

- 2026-11-02: 起票。
- 2026-11-06: 状態: 実装中 — 設計 v0.5 を承認 (2026-11-06)。docs/superpowers/specs/2026-11-06-display-timezone-design.md。表記 2026-11-06 19:04:17 +09:00、内部 UTC、既存 display_timezone を使う
- 2026-11-07: 是正内容 — 2026-11-07 配備 (main eca50b88)。人が見る日時を display_timezone で換算し、表記は YYYY-MM-DD HH:MM:SS ±HH:MM (UTC も +00:00)、zone 名は status / web の見出しだけ。afx-ops は同梱 tzdata で隔離下でも換算、bridge のログは UTC 表記。本機で ctl status の見出し Asia/Tokyo (+09:00) と bridge.log の UTC 表記を確認
