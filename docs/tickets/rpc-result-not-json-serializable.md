---
id: rpc-result-not-json-serializable
status: 実装中
priority: 未設定
opened: 2026-09-03
closed: null
related: [opencode-mcp-timeout-60s]
backfilled: true
source_section: 未レビュー束
---
# [rpc-result-not-json-serializable]

**状態**: 実装中 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[rpc-result-not-json-serializable]/[opencode-mcp-timeout-60s] **是正済 81a2158 2026-09-03、未レビュー** — run_backtest 応答を `{scope,pair,timeframe,source,metrics,trial_count}` に限定 (台帳用 save_kwargs は `_BacktestReply` 属性で運ぶ、period/now は遮断 7 どおり非公開) / 親 dispatcher が write_frame の TypeError/ValueError で死なず "rpc result not serializable" を返す / opencode.json `mcp.afx.timeout` = backtest_rpc_timeout_sec*1000+5000。実装 codex、**指揮者是正: codex が遮断 7 の `_FORBIDDEN_KEYS` から period を外していた (指揮者仕様が誘発) → 撤回**。変異 1 件 red、3184 passed。旧内容: handler が datetime 入り save_kwargs を返し親 json.dumps TypeError で dispatcher 死 (m52、15 秒 timeout が隠していた)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

