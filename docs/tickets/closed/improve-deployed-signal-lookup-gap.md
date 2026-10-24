---
id: improve-deployed-signal-lookup-gap
title: 改善 agent が毎周「get_signals 相当の tool が無い」で止まる穴を塞ぐ
status: 是正済
priority: 中
opened: 2026-10-24
closed: 2026-10-24
related: [improve-context-family-attempt-summary]
---
# [improve-deployed-signal-lookup-gap] 改善 agent が毎周「get_signals 相当の tool が無い」で止まる穴を塞ぐ

**状態**: 是正済 / **優先**: 中

## 現象

get_signals は取引判断 loop 専用で改善ループには IMPROVE_FORBIDDEN (backtest 成績を読み取り tool 経由で材料にさせない設計)。しかし改善 agent は毎周「配備済み strategy の直近 signal 方向を照合したい」と試み、note に同文を 5 回以上残している (backlog #107 等)。設計上の不在が agent に伝わっていないため探索の歩数と note を浪費する。処置案: (a) prompt で不在と理由を明示して試行を止める、(b) 成績を含まない方向だけの読み取り tool を別名で与える、のどちらかを improve-context-family-attempt-summary と同じ束で設計する。2026-10-24 ユーザー承認 (着手順 3)

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-24: 改善 mission の prompt の規律 8 に、get_signals は取引判断 loop 専用で改善では使えないこと、配備済み strategy の直近 signal を材料にしないこと、設計上の制約なので note / discovery に記録しないこと、過去の note の要望は解消済みであることを §4.2 の基準文面どおり明記 (設計書 2026-10-24-improve-feedback-structure-design v0.3b)。pin: 文面の全文一致、IMPROVE_FORBIDDEN 不変、registry exact set 不変 (既存)。事後観測 (AC-20) は次の土曜の改善周で、2 周続いて言及が残れば案 (b) を再検討

## 経緯

- 2026-10-24: 起票。
- 2026-10-24: 状態: 実装待ち — 同 spec §4 で案 (a) = prompt で不在と理由を明示、新 tool なし。T2 (小) が本チケット。2 周続いて言及が残れば案 (b) を再検討
- 2026-10-24: 是正内容 — 改善 mission の prompt の規律 8 に、get_signals は取引判断 loop 専用で改善では使えないこと、配備済み strategy の直近 signal を材料にしないこと、設計上の制約なので note / discovery に記録しないこと、過去の note の要望は解消済みであることを §4.2 の基準文面どおり明記 (設計書 2026-10-24-improve-feedback-structure-design v0.3b)。pin: 文面の全文一致、IMPROVE_FORBIDDEN 不変、registry exact set 不変 (既存)。事後観測 (AC-20) は次の土曜の改善周で、2 周続いて言及が残れば案 (b) を再検討
