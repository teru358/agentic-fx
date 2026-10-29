---
id: weekly-improve-rebacktests-deployed-strategies
title: 週次改善の冒頭で稼働中の戦略を再 backtest し、成績を改善の方向性の材料にする
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-25
closed: null
related: [improve-daily-params-weekly-algorithm, baseline-replay-unimplemented, improve-feedback-structure, same-name-candidate-must-beat-deployed-baseline]
---
# [weekly-improve-rebacktests-deployed-strategies] 週次改善の冒頭で稼働中の戦略を再 backtest し、成績を改善の方向性の材料にする

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

稼働中 (配備済み・有効) の戦略は配備時の backtest 成績しか持たず、相場が変わっても気づけない (陳腐化)。週次の戦略改善 mission は新候補を探すだけで、いま動いている戦略の最新成績を見ていない。週次 (日次改善が入れば日次とも兼ね合う) の改善の冒頭で、稼働中の戦略を最新の履歴で再 backtest し、成績の推移 (配備時 → 今) で改善の方向性 (どの族を伸ばす・直す・止める) を定め、調整の材料にしたい (2026-10-25 ユーザー依頼)。

## 処置案・裁定 (2026-10-25、ユーザー了承)

「再 backtest」でなく**稼働中戦略の定期評価**として設計する。

- 同じ履歴の再 backtest は決定論的に同じ数値を返すので新情報は無い。新しく得られるのは配備後に経過した区間の成績 (前方検証) だけで、評価はそこに絞る。
- 主源 = ペーパー運用の実トレード記録 (スプレッド・約定の現実を含む、最も本物の前方データ)。補助 = 配備後区間の前方 backtest (取引数が少ない戦略の補完と、backtest と実運用の乖離 = 実装・データ問題の検出)。
- 単週の数値では方向を決めない (1h 足 1 週間は約 120 本、選別型は数回しか取引しない)。週次で蓄積し、方向性の判断は 4〜12 週のローリング推移で行う。単週は旗だけ。
- 遮断 8: 配備後の前方区間は親ゲートが改善案を評価する holdout に使う区間なので、agent にその数値を見せない。agent には族別の方向性ラベル (維持 / 低下 / 停止候補) だけ、数値は人間 (親専有レポート、Discord 通知 1 行) にだけ出す (族別試行結果の裁定 2026-10-24 と同じ形)。
- cadence: 週次 mission の冒頭 (決定論的コア側、agent 前)。日次分割 (improve-daily-params-weekly-algorithm) が入れば日次に移してよい。
- 行動: 自動停止はしない (kill switch は別系)。「停止候補」も人間承認。

## 原因

## 処置案・裁定

設計で決める点: (a) 再 backtest は決定論的コア側で mission の前に走らせ、結果は族別試行結果 (改善 feedback、2026-10-24 承認) と同じ表に「稼働中の最新成績」として載せる。agent には in_sample 相当の数値だけ (遮断 8: holdout 側の数値・条件は見せない。再 backtest の区間の切り方をここで決める)。(b) 人間向けには親専有レポートと Discord 通知 (improve-start-and-result-to-discord の開始通知に 1 行足す案)。(c) 成績の低下を機械的にどう扱うか (自動停止はしない。drawdown kill switch は別系。方向性の提示と人間承認の材料に留める) はユーザー裁定。(d) baseline-replay-unimplemented (比較基準の replay) と実装を共有できるか。(e) cadence は improve-daily-params-weekly-algorithm の分割に合わせる。

## 修正内容

## 経緯

- 2026-10-25: 起票。
- 2026-10-25: 指揮者の意見 (定期評価として設計、主源はペーパー実績、ローリング判断、agent にはラベルだけ) をユーザーが了承、処置案に反映。
- 2026-10-29: 関連: [improve-daily-params-weekly-algorithm, baseline-replay-unimplemented, improve-feedback-structure, same-name-candidate-must-beat-deployed-baseline] — 2026-10-29 棚卸し: 関連を束ねる先に張る
