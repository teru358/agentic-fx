---
id: writable-provider-quote-chain-ignores-primary
status: 是正済
priority: 中
opened: 2026-09-24
closed: 2026-09-24
related: []
backfilled: true
source_section: 完了ログ
---
# [writable-provider-quote-chain-ignores-primary]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[writable-provider-quote-chain-ignores-primary]** (**解消 2026-09-24、A2-3a、main squash**。重要度: 中〜高、2026-09-24 A2-3 設計下書きで発見、現物確認済 `price_provider.py:117-127`): A2-1b で primary-only にしたのは readonly provider だけで、書き込み可能 provider (scheduler の成行約定価格・day 強制クローズが使う `get_quote`) は `enabled` の chain (MT5 → TD → yfinance) のまま。`primary: mt5` で `yfinance.enabled: true` を残すと、bridge 不通時に quote が無言で yfinance に落ちる (ユーザー裁定「fallback は明示時だけ」に反する)。いまの設定 (primary = yfinance のみ enabled) では影響なし。A2-3 に含めるか単独修正かはレビューで決める。

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

