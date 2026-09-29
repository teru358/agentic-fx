---
id: floor-path-skips-duplicate-metrics
status: 設計待ち
priority: 未設定
opened: 2026-09-13
closed: null
related: [unprofitable-note-hygiene]
backfilled: true
source_section: 未完了
---
# [floor-path-skips-duplicate-metrics]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[floor-path-skips-duplicate-metrics] 裁定済 (c) 現状維持 (2026-09-13、ユーザー承認)**: 質検査はフロア経路に入れない・母集団も広げない。根拠 = (a) はフロア不合格候補が approval 行を作らないため実 DB で無効、(b) は approval-quality I1 で閉じた偽陽性を再導入 (実例 #83 中間候補 209/1.4701、pin F4-8 red)。遮断 8 例外の再評価 (spec v1.5) で原因は「1 bit が agent 自筆 note #74「提出条件を満たした」に負けた」= note 衛生の問題と判明。材料 `tmp/floor-dup-ticket/{materials,reevaluation}.md`。**後継 ticket → [unprofitable-note-hygiene]**

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

