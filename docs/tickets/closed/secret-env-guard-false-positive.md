---
id: secret-env-guard-false-positive
status: 是正済
priority: 中
opened: 2026-09-20
closed: 2026-10-05
related: []
backfilled: true
source_section: 未完了
---
# [secret-env-guard-false-positive]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[secret-env-guard-false-positive] 起票 2026-09-20 (重要度: 中、案 A 準備の実機で発覚)**: 改善 backend を CLI 系 (claude / codex) にすると、起動時の検査⑤ (`service.py:270-292`、`_SECRET_ENV_PATTERNS` の部分一致) が `~/.bashrc` で export している `CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS` (llama-swap のコンテキスト長の表、秘密ではない) を秘密名と誤判定して起動拒否。暫定 = `env -u CLAUDE_CODE_OPENAI_CONTEXT_WINDOWS uv run python main.py`。処置案 = 既知の非秘密名の許可リスト / 部分一致の見直し (どのパターンに当たったかを特定してから) / エラー文言の `improve+claude` を実際の backend 名に

## 修正内容

- 2026-10-05: 完全一致の service.secret_env_allowlist と一致名だけの warning は実装済み。build_app 経由で完全一致のみ除外され部分一致・大小違い・前置は拒否されることを tests/test_service_wiring_pins.py で pin (許可の部分一致化を変異で確認)

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-05: 是正内容 — 完全一致の service.secret_env_allowlist と一致名だけの warning は実装済み。build_app 経由で完全一致のみ除外され部分一致・大小違い・前置は拒否されることを tests/test_service_wiring_pins.py で pin (許可の部分一致化を変異で確認)
