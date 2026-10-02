---
id: legacy-oc-qwen3-8-toolcall
title: モデルが壊れた引数でツールを連打し予算を焼く
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-oc-qwen3-8-toolcall]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-qwen3.8-toolcall] m20 primary 960 秒の中身 (transcript 初観測): tool_use 94 件中 82 失敗 (87%)。afx_analyze_corr を 67 回、無関係な空文字フィールド (`agent`/`mime`/`sig`/`key`…) を詰めた壊れ payload で連打し invalid_request ループに予算を焼いた。モデルの tool 引数生成が壊れている — tool エラー応答に「正しい引数形の実例」を返す改善が候補

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: モデルが壊れた引数でツールを連打し予算を焼く — 仕分け (2026-10-02、現物で成立を確認): エラー応答に引数形の実例を返す案が候補。 根拠: improve_staging_tools.py の tool エラー応答に正しい引数形の実例を返す処置は見当たらない。
