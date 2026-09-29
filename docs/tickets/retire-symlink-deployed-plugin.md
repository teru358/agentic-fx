---
id: retire-symlink-deployed-plugin
status: 設計待ち
priority: 未設定
opened: 2026-09-19
closed: null
related: [indicator-initial-set]
backfilled: true
source_section: 未完了
---
# [retire-symlink-deployed-plugin]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[retire-symlink-deployed-plugin] 起票 2026-09-19 ([indicator-initial-set] プラン起草時に判明): `afx plugin retire` (`switch.retire_plugin`) は **plain ディレクトリ配備専用**で、bless / approve 経由の symlink 配備 (`.versions/` 方式 = 現在の全配備物) を `ValueError` で拒否する。さらに**依存 strategy を検査しない**ので、退役できたとしても、その indicator を pin した strategy は次の inventory 構築で `not_found` になり黙って外れる。必要な機能 = symlink 配備の退役 + 退役前に依存 strategy を表示して確認。当面、配備済 `rsi_indicator` / `rsi_wilder` は新 `rsi` と併存させる

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

