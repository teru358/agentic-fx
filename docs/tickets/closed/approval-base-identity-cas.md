---
id: approval-base-identity-cas
title: 承認時に候補の土台版を照合し、同名候補の上書き (lost update) を防ぐ
status: 是正済
priority: 高
kind: 不具合
opened: 2026-10-27
closed: 2026-11-08
related: [ctl-improve-repeat-duration-and-oneoff-comment, same-name-candidate-must-beat-deployed-baseline]
---
# [approval-base-identity-cas] 承認時に候補の土台版を照合し、同名候補の上書き (lost update) を防ぐ

**状態**: 是正済 / **優先**: 高 / **種別**: 不具合

## 現象

同じ配備版 v1 から作られた同名 plugin の候補 A・B が両方承認待ちのとき、古い A → 新しい B の順に承認すると B が A の変更を黙って消す。_is_superseded は「後の ID が承認済み」だけを防ぐ (plugin/switch.py)。承認依頼 payload に候補の土台 hash が無い。承認画面 (ops/change.py) は閲覧時の live と動的比較するが巻き戻りの警告は無い。afx ctl improve の繰り返しで提出後も mission が続くため起きやすくなる。案 (sol 2026-10-27): 提出時に live identity (base hash か absent) を payload に保存し、承認 lock 内で現在の live と CAS 照合、不一致は拒否して再 base・再提出を求める。提出時の同名 pending 拒否は補助。決めること: legacy payload の扱い、rollback・human bless 経路、既存 pending の扱い。最初に v1 から A/B を提出し A→B 承認する最小 test で現状を固定する。

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-08: 承認依頼の payload に土台 (base: version / plain / none / unchecked) を記録し、approve・retry が live と照合して違えば invalidated / base_changed にする。改善 mission は開始時のスナップショット、人間の提出は提出時の live (版 dir が欠損・空でも受け付け、root 外は拒否)、bless は照合しない印。ctl・shell・web・Discord に土台の警告と無効の案内を出し、Approve / Retry の確認文にも警告を添える。レビュー: codex sol 1 周目 (Important 3 是正)、/code-review high (8 件却下)、codex 閉鎖 2 回。判定: plugin 2877 / loops 2121 / ops 2527 / afx-ops 3889 ほか tests/ 全体で既知の失敗のみ

## 経緯

- 2026-10-27: 起票。
- 2026-10-29: 優先: 高、関連: [ctl-improve-repeat-duration-and-oneoff-comment, same-name-candidate-must-beat-deployed-baseline] — 2026-10-29 棚卸し: 承認の中身の正しさ: 同名候補 A→B 承認で B が A を黙って消す。同名更新が増える (plugin-naming 方針・比較 gate) 前に塞ぐ
- 2026-11-07: 状態: 実装中 — 設計 C1 (docs/superpowers/specs/2026-11-07-approval-base-identity-cas-design.md) を 2026-11-07 に利用者承認、§7 の裁定 3 点は推奨どおり。T1 (土台の保存と照合) → T2 (承認画面の土台の項目)
- 2026-11-08: 是正内容 — 承認依頼の payload に土台 (base: version / plain / none / unchecked) を記録し、approve・retry が live と照合して違えば invalidated / base_changed にする。改善 mission は開始時のスナップショット、人間の提出は提出時の live (版 dir が欠損・空でも受け付け、root 外は拒否)、bless は照合しない印。ctl・shell・web・Discord に土台の警告と無効の案内を出し、Approve / Retry の確認文にも警告を添える。レビュー: codex sol 1 周目 (Important 3 是正)、/code-review high (8 件却下)、codex 閉鎖 2 回。判定: plugin 2877 / loops 2121 / ops 2527 / afx-ops 3889 ほか tests/ 全体で既知の失敗のみ
