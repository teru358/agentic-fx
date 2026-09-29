---
id: legacy-oc-websearch-ssl
status: 設計待ち
priority: 未設定
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-oc-websearch-ssl]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-websearch-ssl] m26 で `afx_web_search` 10 回中 5 回 `DDGSException: SSL: CERTIFICATE_VERIFY_FAILED` (同 mission 内で 4 回は成功、m24 は 2/2 成功) — 間欠。Landlock 起因ではない (/etc は read allowlist、成功例あり)。DDG 側の遮断/レート挙動の疑い。research の max_searches=20 は外向き予算として妥当か再確認

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

