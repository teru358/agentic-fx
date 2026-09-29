---
id: legacy-oc-tool-not-found
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-oc-tool-not-found]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-tool-not-found] m24/m26 で `read_staging_file`/`read_plugin_source` の `not found` が 7-11 回 (staging に書く前に読む、`_examples/rsi_indicator` を read_plugin_source に渡す等)。tool エラー応答に「存在しない候補名。list_staging で確認 / write_staging_file で先に作る」等の誘導文を返す案 ([OC-qwen3.8-toolcall] と同系)。ed0f0f1 で not found に hint 追加済み (部分対応)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

