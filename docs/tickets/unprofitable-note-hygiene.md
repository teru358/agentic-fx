---
id: unprofitable-note-hygiene
status: 裁定待ち
priority: 未設定
opened: 2026-09-13
closed: null
related: [index-best-shows-in-sample-pf-on-unprofitable]
backfilled: true
source_section: 未完了
---
# [unprofitable-note-hygiene]

**状態**: 裁定待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[unprofitable-note-hygiene]** (2026-09-13 起票、小束、設計待ち): フロア不合格 (`unprofitable`) で終わった mission が起票した agent note (#70/#71/#74 型) が in_sample の成功語彙で残り、次 mission が同一候補を再提出する (run19 #84)。是正 2 点 (両方): (1) フロア不合格 mission 起票の note に機械由来の注記「この mission の候補は unprofitable」を付ける (起票箇所 = `_finalize_gate_failed` 経路の backlog note 書き込み) (2) `improve_mission.md` の規律文に「`unprofitable` で終わった課題と同型の候補を再提出しない」を追加。遮断 8 の語彙・母集団は不変。pin: note 本文に注記が付く / prompt に規律文が出る / 質検査母集団は F4-8 のまま- **[index-best-shows-in-sample-pf-on-unprofitable]** (2026-09-13 A4 19 観測 C、低): archive INDEX の `best=… pf=1.470` は in_sample 値で、status `unprofitable` 行と並ぶと「pf 1.47 なのに不採算」と読める。人間導線の表記整理 (段名を付ける等)。裁定待ち

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

