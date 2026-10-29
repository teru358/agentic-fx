---
id: approval-base-identity-cas
title: 承認時に候補の土台版を照合し、同名候補の上書き (lost update) を防ぐ
status: 設計待ち
priority: 高
kind: 不具合
opened: 2026-10-27
closed: null
related: [ctl-improve-repeat-duration-and-oneoff-comment, same-name-candidate-must-beat-deployed-baseline]
---
# [approval-base-identity-cas] 承認時に候補の土台版を照合し、同名候補の上書き (lost update) を防ぐ

**状態**: 設計待ち / **優先**: 高 / **種別**: 不具合

## 現象

同じ配備版 v1 から作られた同名 plugin の候補 A・B が両方承認待ちのとき、古い A → 新しい B の順に承認すると B が A の変更を黙って消す。_is_superseded は「後の ID が承認済み」だけを防ぐ (plugin/switch.py)。承認依頼 payload に候補の土台 hash が無い。承認画面 (ops/change.py) は閲覧時の live と動的比較するが巻き戻りの警告は無い。afx ctl improve の繰り返しで提出後も mission が続くため起きやすくなる。案 (sol 2026-10-27): 提出時に live identity (base hash か absent) を payload に保存し、承認 lock 内で現在の live と CAS 照合、不一致は拒否して再 base・再提出を求める。提出時の同名 pending 拒否は補助。決めること: legacy payload の扱い、rollback・human bless 経路、既存 pending の扱い。最初に v1 から A/B を提出し A→B 承認する最小 test で現状を固定する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-27: 起票。
- 2026-10-29: 優先: 高、関連: [ctl-improve-repeat-duration-and-oneoff-comment, same-name-candidate-must-beat-deployed-baseline] — 2026-10-29 棚卸し: 承認の中身の正しさ: 同名候補 A→B 承認で B が A を黙って消す。同名更新が増える (plugin-naming 方針・比較 gate) 前に塞ぐ
