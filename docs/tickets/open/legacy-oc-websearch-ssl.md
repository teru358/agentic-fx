---
id: legacy-oc-websearch-ssl
title: web 検索が間欠的に SSL 検証失敗する
status: 設計待ち
priority: 低
opened: null
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [legacy-oc-websearch-ssl]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

[OC-websearch-ssl] m26 で `afx_web_search` 10 回中 5 回 `DDGSException: SSL: CERTIFICATE_VERIFY_FAILED` (同 mission 内で 4 回は成功、m24 は 2/2 成功) — 間欠。Landlock 起因ではない (/etc は read allowlist、成功例あり)。DDG 側の遮断/レート挙動の疑い。research の max_searches=20 は外向き予算として妥当か再確認

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: web 検索が間欠的に SSL 検証失敗する — 仕分け (2026-10-02、要確認 (再現・実機観測などが要る)): DDG 側の遮断・レートの疑い、外向き予算の再確認が残る。 根拠: afx_web_search の DDG 間欠 SSL 失敗は実測のみ。config.py:311 max_searches=20 の妥当性は再確認が要る。
