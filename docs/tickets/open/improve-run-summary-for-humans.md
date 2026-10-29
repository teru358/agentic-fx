---
id: improve-run-summary-for-humans
title: 改善周ごとの要約 (仮説・使った指標・床との差) を人間に見せる
status: 設計待ち
priority: 高
opened: 2026-10-25
closed: null
related: [improve-objective-filtered-signal-quality, bot-event-notices-are-raw-log-lines, improve-mission-output-not-persisted]
---
# [improve-run-summary-for-humans] 改善周ごとの要約 (仮説・使った指標・床との差) を人間に見せる

**状態**: 設計待ち / **優先**: 高 / **種別**: 機能

## 現象

改善ループは動いて候補を作っているが、提出床 (PF ≥ 1.0) 未達の試行は backlog の observation / note 行にしか残らず、承認待ちにも plugin にも現れない。利用者は「改善が行われているか」を読み取れない (2026-10-25、手動 improve 5 回で候補 5 本すべて床未達、承認 0)。案: 改善 mission 1 回ごとに「試した仮説 / 使った指標 (新設した indicator を含む) / 床との差 (PF・取引数など) / 次回への申し送り」を 1 ブロックに要約し、通知 (notify) と同梱 web の 1 ページに出す。backlog の note は現状維持。要約文は改善 mission の出力から決定論的に組み立て、LLM の自由記述は引用に限る。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
- 2026-10-29: 優先: 高、関連: [improve-objective-filtered-signal-quality, bot-event-notices-are-raw-log-lines, improve-mission-output-not-persisted] — 2026-10-29 棚卸し: 道筋 2 番目「承認の中身を見せる」の核: 床未達の試行が承認待ちにも plugin にも現れず、改善が行われているか読めない。優先を低から引き上げ提案
