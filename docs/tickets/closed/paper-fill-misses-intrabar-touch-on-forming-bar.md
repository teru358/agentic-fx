---
id: paper-fill-misses-intrabar-touch-on-forming-bar
status: 是正済
priority: 中
opened: 2026-09-22
closed: 2026-09-21
related: [closed-bars-and-required-window]
backfilled: true
source_section: 完了ログ
---
# [paper-fill-misses-intrabar-touch-on-forming-bar]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[paper-fill-misses-intrabar-touch-on-forming-bar]** (**解消 2026-09-22、A2-1a、main squash commit。導入時に ohlcv_cache を全削除するので再起動は開場中か建玉なしの時**) (重要度: 中〜高、2026-09-21 A2 設計レビュー r1 codex sol、未実測): ライブの paper 約定判定は `latest_1m_bar()` の末尾 = 形成中の 1m 足を読み、同じ `bar.ts` を最初の観測で処理済みにする (`core/scheduler.py:346-353,1021-1029`)。その 1 分の残りで SL / limit に到達しても見逃し得る。mark-to-market / HWM も未確定 close で揺れる。LLM tools (`get_ohlcv` `get_indicators`) も形成中の足を含む系列を受ける (strategy plugin は確定 bucket だけ読むので該当しない)。A2-1 [closed-bars-and-required-window] の「確定足の契約」で解消予定 (`tmp/design-a2/r1/verdicts.md`)。

## 修正内容

原文に commit・レビュー結果・実機受入が含まれる場合はそれが正。当時の詳細記録は `tmp/review-*/`・`tmp/design-*/` (gitignore) と git log。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

