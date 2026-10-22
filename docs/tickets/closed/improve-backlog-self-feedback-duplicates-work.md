---
id: improve-backlog-self-feedback-duplicates-work
title: 改善 mission が自分の成果報告を backlog に書き戻し、次回に同じ戦略を再実装する
status: 是正済
priority: 中
opened: 2026-10-10
closed: 2026-10-22
related: [improve-objective-filtered-signal-quality]
---
# [improve-backlog-self-feedback-duplicates-work] 改善 mission が自分の成果報告を backlog に書き戻し、次回に同じ戦略を再実装する

**状態**: 是正済 / **優先**: 中

## 現象

mission 1061 (2026-10-10 08:00 JST) が bb_ema_breakout を候補化した後、その成果文「Bollinger・EMA を組み合わせた未試行のブレイクアウト戦略を実装した」が source=agent の backlog #92 として登録され、次の mission 1062 (手動) はこれを課題として選び、同じ BB 20/2.0 + EMA 50 の bb_ema_slope_breakout を 2 候補作った。metrics は 1061 の候補と完全一致 (PF 1.021 / 277 trades) で、重複検知 (duplicate_metrics_of) が observation に落として承認には進まなかったが、codex 枠と 4.6 分を消費した。原因候補: 成果の自己申告 (agent_claims) を backlog の idea として採用している / backlog の選択が origin_mission の直近成果を除外していない。処置案: 成果報告由来の idea は「次の課題」(未試行の仮説) だけを採り「実装した」系の文は登録しない / 選択時に origin_mission が直前で、同じ indicator 構成の候補が archive にあるものを後回しにする。

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-22: spec 2026-10-19-improve-loop-quality-design §2.1 (A) を IQ-T1 (main 6eeeb67 系、merge 2026-10-21) で実装。discoveries は note のみで open 行を作れず、task kind は廃止、agent が task を足せる経路は選択済みの selected.idea だけ (AC-1〜4)。backlog の open writer は allowlist を AST 走査で pin (AC-3)。context の backlog / note の last_result は固定語彙に投影し holdout 等の数値が漏れない。本番反映 2026-10-22 の daemon 再起動。運用での再発 0 の確認 (spec §3.2 不変条件 2) は 10/24 以降の改善周で見る

## 経緯

- 2026-10-10: 起票。
- 2026-10-19: 状態: 実装中 — 設計 docs/superpowers/specs/2026-10-19-improve-loop-quality-design.md v1.0 をユーザー承認 (2026-10-19)。IQ-T0 (台帳 schema) から着手
- 2026-10-22: 是正内容 — spec 2026-10-19-improve-loop-quality-design §2.1 (A) を IQ-T1 (main 6eeeb67 系、merge 2026-10-21) で実装。discoveries は note のみで open 行を作れず、task kind は廃止、agent が task を足せる経路は選択済みの selected.idea だけ (AC-1〜4)。backlog の open writer は allowlist を AST 走査で pin (AC-3)。context の backlog / note の last_result は固定語彙に投影し holdout 等の数値が漏れない。本番反映 2026-10-22 の daemon 再起動。運用での再発 0 の確認 (spec §3.2 不変条件 2) は 10/24 以降の改善周で見る
