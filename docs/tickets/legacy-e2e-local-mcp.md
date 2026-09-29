---
id: legacy-e2e-local-mcp
status: 設計待ち
priority: 未設定
opened: 2026-08-30
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-local-mcp]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-local-mcp]/[E2E-qwen3.8→確定 2026-08-30] codex+llama_swap で MCP tool が使われない (qwen3-coder が write_staging_file を shell で実行、mission #6) 真因 = **codex は MCP tools を Responses API の `type:"namespace"` 集約形式 (`mcp__afx` 1 個に 7 tool 内包) で送っており、llama.cpp の /v1/responses が namespace 型をテンプレート展開できない** (透過プロキシで request body 実測)。`wire_api=chat` は codex 0.150.x で廃止済みで逃げ道なし。対応候補: (a) llama.cpp の namespace tool 対応を待つ/起票 (b) namespace→function 展開の変換プロキシ (c) local backend (自前 tool loop)。→ ローカルハーネスは opencode に裁定 (メモリ `local-llm-harness-is-opencode`)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

