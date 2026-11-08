---
id: reevaluation-approval-detail-display-polish
title: 再評価の承認詳細の数値が丸められず、旧形式の注記と分類「不明」が出る
status: 是正済
priority: 低
kind: 保守
opened: 2026-11-07
closed: 2026-11-08
related: [redo-deployed-strategy-evaluation-after-time-repair, backtest-scheduler-log-leak]
---
# [reevaluation-approval-detail-display-polish] 再評価の承認詳細の数値が丸められず、旧形式の注記と分類「不明」が出る

**状態**: 是正済 / **優先**: 低 / **種別**: 保守

## 現象

2026-11-07 の本番の再評価 (#89〜#94) の承認詳細で次を観測。(1) pf などが pf=1.0165433487012487 のように丸められない。(2) 再評価の依頼にも「旧形式の承認依頼です.親の事実表は保存されていません」の注記が出る。(3) 承認後の #89 だけ分類が「不明」(他は同一版)。(4) 評価中に maintain_reservations failed: ValueError: no completed 5m bar for rate conversion at 2026-06-02T21:25:00Z の警告 1 行 (評価は完走)

## 原因

## 処置案・裁定

2026-11-08 利用者承認。
1. 再評価の表の数値を Discord と同じ規則 (float は有効 4 桁 `.4g`、int はそのまま) で丸める。本体 `ops/reevaluation_view.py` (ctl・shell・web) と afx-ops `reevaluation_text.py` の 2 箇所。通常の承認詳細 (ctl・web) の丸めは範囲外 (残余)。
2. 再評価の依頼には事実表の欄 (旧形式の注記と自己申告) を出さない。親が測り直した依頼で agent の自己申告が無く、数値は再評価の表にある。
3. 決定後の再評価の依頼は分類「同一版」。再評価は配備中の版を測り直す依頼で切替 journal を作らない。承認待ちの間の「同一版」と揃える。
4. 換算の警告は backtest-scheduler-log-leak に統合済み。

## 修正内容

- 2026-11-08: 再評価の表の数値を Discord と同じ規則 (float は有効 4 桁、int はそのまま、長さ上限は daemon と同じ) で丸める (ops/reevaluation_view.py、afx-ops reevaluation_text.py)。再評価の依頼には事実表の欄を出さない (approval_facts.render_facts_lines)。決定後 (approved・rejected・invalidated・expired) の再評価の依頼は分類「同一版」、候補は版の置き場から読む (ops/change.py)。残余: 通常の承認詳細の数値は ctl・web で丸めていない (範囲外)。レビュー: codex terra 1 周目 (2 件採用)、/code-review high (7 件中 2 件採用)、codex 閉鎖で指摘なし。判定: ops 2538 / loops 2121 / plugin 2877 / afx-ops 3891 ほか、既知の失敗のみ

## 経緯

- 2026-11-07: 起票。
- 2026-11-07: 関連: [redo-deployed-strategy-evaluation-after-time-repair, backtest-scheduler-log-leak] — 4 点目 (換算の警告) は backtest-scheduler-log-leak に統合。残り 3 点の原因: (1) 再評価の表示 (ops/reevaluation_view.py:110-113、afx-ops reevaluation_text.py:59-63) が数値を丸めない (通常経路の ctl / web も丸めない、丸めは Discord の gate/render.py:104-108 だけ) (2) 再評価の依頼は facts_version を持たず approval_facts.py:367-386 で旧形式扱い (3) 決定後の分類は自分の approval id の切替 journal を前提にし (ops/change.py:122-124)、再評価の承認は journal を作らないので承認した 4 本 (#89・91・92・94) すべて不明
- 2026-11-08: 状態: 実装中 — 処置案を利用者が承認し着手した
- 2026-11-08: 是正内容 — 再評価の表の数値を Discord と同じ規則 (float は有効 4 桁、int はそのまま、長さ上限は daemon と同じ) で丸める (ops/reevaluation_view.py、afx-ops reevaluation_text.py)。再評価の依頼には事実表の欄を出さない (approval_facts.render_facts_lines)。決定後 (approved・rejected・invalidated・expired) の再評価の依頼は分類「同一版」、候補は版の置き場から読む (ops/change.py)。残余: 通常の承認詳細の数値は ctl・web で丸めていない (範囲外)。レビュー: codex terra 1 周目 (2 件採用)、/code-review high (7 件中 2 件採用)、codex 閉鎖で指摘なし。判定: ops 2538 / loops 2121 / plugin 2877 / afx-ops 3891 ほか、既知の失敗のみ
