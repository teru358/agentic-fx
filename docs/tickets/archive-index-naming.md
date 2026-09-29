---
id: archive-index-naming
status: 裁定待ち
priority: 未設定
opened: 2026-09-12
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [archive-index-naming]

**状態**: 裁定待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[archive-index-naming] (裁定待ち、同 観測 B) `plugins/_archive/INDEX.md` は approval 終端を載せず GC も更新しないので目録ではない (非承認終端のログ)。名前を変える (`TERMINATIONS.md`) か approval も 1 行書くか。観測 E: `mission_outcome` は「この行を作った mission の終端」であって「この候補が承認された」ではない (設計書に明記) (再確認 #74/#75/#76、2026-09-12) **→ 裁定 2026-09-12: approval も INDEX に載せる (`7837fe9`)。実機で approval 行は未観測 (run15 は observation/report)。既存 INDEX.md にヘッダ注記は付かない (軽微、据え置き)**

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

