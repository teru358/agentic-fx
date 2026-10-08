---
id: legacy-oc-tool-not-found
title: 存在しない候補名を読むときの誘導が足りない
status: 実装待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-oc-tool-not-found]

**状態**: 実装待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-tool-not-found] m24/m26 で `read_staging_file`/`read_plugin_source` の `not found` が 7-11 回 (staging に書く前に読む、`_examples/rsi_indicator` を read_plugin_source に渡す等)。tool エラー応答に「存在しない候補名。list_staging で確認 / write_staging_file で先に作る」等の誘導文を返す案 ([OC-qwen3.8-toolcall] と同系)。ed0f0f1 で not found に hint 追加済み (部分対応)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 低、題名: 存在しない候補名を読むときの誘導が足りない — 仕分け (2026-10-02、現物で成立を確認): not found 応答の hint は部分対応。list_staging への誘導文が残る。 根拠: ed0f0f1 で hint 追加済み。improve_staging_tools.py:258,280,320 の not found は available を返すが、誘導文は部分的。
