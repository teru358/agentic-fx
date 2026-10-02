---
id: intent-evidence-timeframe-gate
title: 判断足と無関係な足を根拠にした OPEN を gate が弾かない
status: 設計待ち
priority: 低
opened: 2026-09-27
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [intent-evidence-timeframe-gate]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[intent-evidence-timeframe-gate] 起票 2026-09-27 (B-2 段 d の spec §2 で範囲外に切り出し、設計待ち)**: `role` (primary / context / other) は market tool 応答に足の意味を表示するだけで、executor は TradeIntent がどの足を根拠にしたか検証しない (`executor.py:435-481`)。LLM が context 足だけを理由に OPEN を返しても gate は弾かない。構造的に防ぐには TradeIntent に根拠足 (evidence timeframe) を記録し Risk Gate で判断足と照合する別設計が要る。現状は「表示で抑制」まで

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 判断足と無関係な足を根拠にした OPEN を gate が弾かない — 仕分け (2026-10-02、現物で成立を確認): TradeIntent に根拠足を持たせて照合する別設計が必要。現状は表示で抑制。 根拠: executor.py:435-481 付近に根拠足の検証は無く、evidence_timeframe の語も src に存在しない。
