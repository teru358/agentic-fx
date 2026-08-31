"""Literal fixtures captured from the m30 OpencodeRunner mission transcript."""

M30_TEXT = '''I've established the key facts through repeated attempts:

1. **The examples directory does not exist** at `/home/teru358/project/agentic-fx/plugins/_staging/30/_snapshot_src/_examples/` (confirmed via `run_backtest` filesystem error and `read_staging_file` "not found").
2. **The staging area is empty** (`list_staging` returned `{"candidates": []}`).
3. **Web search cannot find the specific agentic-fx project** — only generic trading tools.

This matches the exact blocker documented in backlog #1 (attempts 5/5): without access to `_examples`, the allowed config keys of `_validate_config` can't be pinned down, and a re-submission would very likely fail again with `gate_failed:loader_rejected: unknown config keys`.

I did verify one thing earlier that's worth preserving: a minimal `kind: indicator`-only config passes `run_plugin_tests`, meaning the loader rejects extra keys like `name`/`period` — but I couldn't confirm the full allowed schema. Given the hard blocker, the disciplined move is to record an observation rather than retry into the same rejection.

{
  "discoveries": [
    {
      "idea": "RSI の有効性は市場環境依存: トレンド相場ではトレンド・プルバック (200EMA 上 + RSI が 45 を下抜け後に再上抜け)、レンジ相場では 70/30 リバージョン",
      "source": "research",
      "evidence": "過去リサーチで市場環境別の方針が得られた: トレンド相場 (ADX 高) は 200EMA 上のトレンド・プルバック、レンジ相場 (ADX<22) は 70/30 リバージョンが有効。ただしこれは indicator plugin 実装が前提。"
    },
    {
      "idea": "RSI の期間 21 はレンジ相場 (EUR/USD) では逆効果: バックテストで RSI(21) リバージョンの profit factor が 0.62",
      "source": "research",
      "evidence": "バックテスト結果: EUR/USD で RSI(21) リバースンの PF=0.62 と損失圏。期間 21 の優先度は下げるべき。"
    }
  ],
  "selected": {
    "backlog_id": 1,
    "idea": "implement an RSI indicator plugin modeled on docs/examples/plugins/rsi_indicator (period 14, Wilder smoothing: seed with SMA then avg=(prev*(period-1)+cur)/period), minimal and read-only"
  },
  "artifact": {
    "type": "observation",
    "reason": "examples ディレクトリ (_snapshot_src/_examples/) がディスク上に存在せず (run_backtest で [Errno 2]、read_staging_file で not found)、staging 領域も空 (list_staging: [])。web_search でも agentic-fx プロジェクトの正確な config スキーマは確認できなかった。これは backlog #1 の attempts 5/5 で毎回 'gate_failed:loader_rejected: unknown config keys' で却下されたのと同じ構造的ブロッカー。検証済みの事実: (1) kind: indicator のみの最小 config は run_plugin_tests を通過する → loader が拒否したのは 'name'/'period' 等の余分なキー。(2) plugin.py の compute(candles, params)->list シグナチャ自体は test 通過確認済み。次回への申し送り: _examples に実サンプルが無い場合、親プロセスに _validate_config の許可キー一覧 (kind / params / warmup 等) を確認してもらうか、params: {} 内に period を渡す形式を試す。許容キーが判明すれば RSI(14) Wilder平滑の plugin は実装可能。"
  },
  "selection_rationale": "backlog #1 (RSI indicator plugin) は本ミッションの中心的な未解決課題だが、examples がアクセス不可という構造的ブロッカーにより config スキーマを特定できず再提出しても同じ却下理由になる可能性が高い。規律3に従い observation として理由と申し送りを残す。研究系の发现 (市場環境別方针 #8、期間21の逆効果 #7) は次回の実装・バックテストへ引き継ぐ。"
}'''

M30_TEXT_EVENT_LINE = '''{"type":"text","timestamp":1788178377447,"sessionID":"ses_fa844dd9cffePfxeKnM3xBcJlb","part":{"id":"prt_057bc7df2001FJ2sL9OliujFRp","messageID":"msg_057bc7c3f001fc4Mlna5UflXAa","sessionID":"ses_fa844dd9cffePfxeKnM3xBcJlb","type":"text","text":"I've established the key facts through repeated attempts:\\n\\n1. **The examples directory does not exist** at `/home/teru358/project/agentic-fx/plugins/_staging/30/_snapshot_src/_examples/` (confirmed via `run_backtest` filesystem error and `read_staging_file` \\"not found\\").\\n2. **The staging area is empty** (`list_staging` returned `{\\"candidates\\": []}`).\\n3. **Web search cannot find the specific agentic-fx project** — only generic trading tools.\\n\\nThis matches the exact blocker documented in backlog #1 (attempts 5/5): without access to `_examples`, the allowed config keys of `_validate_config` can't be pinned down, and a re-submission would very likely fail again with `gate_failed:loader_rejected: unknown config keys`.\\n\\nI did verify one thing earlier that's worth preserving: a minimal `kind: indicator`-only config passes `run_plugin_tests`, meaning the loader rejects extra keys like `name`/`period` — but I couldn't confirm the full allowed schema. Given the hard blocker, the disciplined move is to record an observation rather than retry into the same rejection.\\n\\n{\\n  \\"discoveries\\": [\\n    {\\n      \\"idea\\": \\"RSI の有効性は市場環境依存: トレンド相場ではトレンド・プルバック (200EMA 上 + RSI が 45 を下抜け後に再上抜け)、レンジ相場では 70/30 リバージョン\\",\\n      \\"source\\": \\"research\\",\\n      \\"evidence\\": \\"過去リサーチで市場環境別の方針が得られた: トレンド相場 (ADX 高) は 200EMA 上のトレンド・プルバック、レンジ相場 (ADX<22) は 70/30 リバージョンが有効。ただしこれは indicator plugin 実装が前提。\\"\\n    },\\n    {\\n      \\"idea\\": \\"RSI の期間 21 はレンジ相場 (EUR/USD) では逆効果: バックテストで RSI(21) リバージョンの profit factor が 0.62\\",\\n      \\"source\\": \\"research\\",\\n      \\"evidence\\": \\"バックテスト結果: EUR/USD で RSI(21) リバースンの PF=0.62 と損失圏。期間 21 の優先度は下げるべき。\\"\\n    }\\n  ],\\n  \\"selected\\": {\\n    \\"backlog_id\\": 1,\\n    \\"idea\\": \\"implement an RSI indicator plugin modeled on docs/examples/plugins/rsi_indicator (period 14, Wilder smoothing: seed with SMA then avg=(prev*(period-1)+cur)/period), minimal and read-only\\"\\n  },\\n  \\"artifact\\": {\\n    \\"type\\": \\"observation\\",\\n    \\"reason\\": \\"examples ディレクトリ (_snapshot_src/_examples/) がディスク上に存在せず (run_backtest で [Errno 2]、read_staging_file で not found)、staging 領域も空 (list_staging: [])。web_search でも agentic-fx プロジェクトの正確な config スキーマは確認できなかった。これは backlog #1 の attempts 5/5 で毎回 'gate_failed:loader_rejected: unknown config keys' で却下されたのと同じ構造的ブロッカー。検証済みの事実: (1) kind: indicator のみの最小 config は run_plugin_tests を通過する → loader が拒否したのは 'name'/'period' 等の余分なキー。(2) plugin.py の compute(candles, params)->list シグナチャ自体は test 通過確認済み。次回への申し送り: _examples に実サンプルが無い場合、親プロセスに _validate_config の許可キー一覧 (kind / params / warmup 等) を確認してもらうか、params: {} 内に period を渡す形式を試す。許容キーが判明すれば RSI(14) Wilder平滑の plugin は実装可能。\\"\\n  },\\n  \\"selection_rationale\\": \\"backlog #1 (RSI indicator plugin) は本ミッションの中心的な未解決課題だが、examples がアクセス不可という構造的ブロッカーにより config スキーマを特定できず再提出しても同じ却下理由になる可能性が高い。規律3に従い observation として理由と申し送りを残す。研究系の发现 (市場環境別方针 #8、期間21の逆効果 #7) は次回の実装・バックテストへ引き継ぐ。\\"\\n}","time":{"start":1788178365938,"end":1788178377433}}}'''
