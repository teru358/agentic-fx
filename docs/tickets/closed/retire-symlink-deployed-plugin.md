---
id: retire-symlink-deployed-plugin
title: afx plugin retire が symlink 配備の指標を退役できない
status: 是正済
priority: 中
opened: 2026-09-19
closed: 2026-10-18
related: [deployed-plugin-has-no-disable-or-rollback]
backfilled: true
source_section: 未完了
---
# [retire-symlink-deployed-plugin]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[retire-symlink-deployed-plugin] 起票 2026-09-19 ([indicator-initial-set] プラン起草時に判明): `afx plugin retire` (`switch.retire_plugin`) は **plain ディレクトリ配備専用**で、bless / approve 経由の symlink 配備 (`.versions/` 方式 = 現在の全配備物) を `ValueError` で拒否する。さらに**依存 strategy を検査しない**ので、退役できたとしても、その indicator を pin した strategy は次の inventory 構築で `not_found` になり黙って外れる。必要な機能 = symlink 配備の退役 + 退役前に依存 strategy を表示して確認。当面、配備済 `rsi_indicator` / `rsi_wilder` は新 `rsi` と併存させる

## 修正内容

- 2026-10-18: plugin deploy ops spec (2026-10-16、v1.1) に統合。afx plugin retire は symlink 配備に当たると disable と同じ状態機械 (op_kind=disable_symlink、依存確認、name lock、cascade、意図の記録 → unlink → done、通知) で退役し、plain dir は retire_plain 状態機械で _retired/ へ退避する。T5 で実装、T4b で crash matrix、T7 で CLI (予告 / --yes / --dry-run / rc と code) を整備し main に merge 済み。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 中、題名: afx plugin retire が symlink 配備の指標を退役できない — 仕分け (2026-10-02、現物で成立を確認): symlink 配備の退役と、退役前の依存 strategy 表示が必要。当面は併存で運用。 根拠: switch.py:1857-1859 が symlink 配備を ValueError で拒否する。依存 strategy の検査も無い。
- 2026-10-04: 関連: [deployed-plugin-has-no-disable-or-rollback] — 2026-10-04: 配備済み plugin の disable / rollback の設計 (deployed-plugin-has-no-disable-or-rollback) に統合して扱う (操作 API spec v1.3 §9)。
- 2026-10-18: 是正内容 — plugin deploy ops spec (2026-10-16、v1.1) に統合。afx plugin retire は symlink 配備に当たると disable と同じ状態機械 (op_kind=disable_symlink、依存確認、name lock、cascade、意図の記録 → unlink → done、通知) で退役し、plain dir は retire_plain 状態機械で _retired/ へ退避する。T5 で実装、T4b で crash matrix、T7 で CLI (予告 / --yes / --dry-run / rc と code) を整備し main に merge 済み。
