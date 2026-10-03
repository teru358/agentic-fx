---
id: selection-rationale-unverified
title: 承認材料の selection_rationale が未検証で捏造され得る
status: 是正済
priority: 高
opened: null
closed: 2026-10-03
related: []
backfilled: true
source_section: 未完了
---
# [selection-rationale-unverified]

**状態**: 是正済 / **優先**: 高

## 現象・原因・処置案 (tickets.md からの移行、原文)

[selection-rationale-unverified] selection_rationale が未検証で payload に載る (m40 は attempts を捏造)。attempts と突き合わせで検出可

## 修正内容

- 2026-10-03: 承認画面 approval <id> に親が受理・記録した内容の欄 (backlog の attempts、過去 run、mission 内の backtest 試行) を固定注記つきで出し、agent の selection_rationale・summary・課題の文面は自己申告 (未検証) 欄に分ける。全表示文字列を無害化、壊れた保存形式でも field 単位で落ちない。main 95217f6 まで (段 0 変異 109 本、レビュー 2 周、spec v1.1)。実機受入は次回の承認依頼で確認。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 高、題名: 承認材料の selection_rationale が未検証で捏造され得る — 仕分け (2026-10-02、現物で成立を確認): attempts との突き合わせで検出可能、m40 で attempts を捏造した実例。 根拠: loops/improve_loop.py:1636 で output の selection_rationale を未検証のまま payload に載せる。
- 2026-10-02: 状態: 実装待ち — spec docs/superpowers/specs/2026-10-02-approval-parent-facts-design.md v1.0。承認画面 (approval <id>) に「親が受理・記録した内容」欄を新設し、agent の選定理由・要約・課題の文面は「agent の自己申告 (未検証)」欄に下げる。機械照合はしない。prompt の出力例から回数を除く。実例: 試行回数の捏造 2 件を確認。現状は承認画面に選定理由が表示されていなかった
- 2026-10-03: 是正内容 — 承認画面 approval <id> に親が受理・記録した内容の欄 (backlog の attempts、過去 run、mission 内の backtest 試行) を固定注記つきで出し、agent の selection_rationale・summary・課題の文面は自己申告 (未検証) 欄に分ける。全表示文字列を無害化、壊れた保存形式でも field 単位で落ちない。main 95217f6 まで (段 0 変異 109 本、レビュー 2 周、spec v1.1)。実機受入は次回の承認依頼で確認。
