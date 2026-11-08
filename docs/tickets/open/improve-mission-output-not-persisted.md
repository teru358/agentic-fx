---
id: improve-mission-output-not-persisted
title: 改善 mission の出力 (output_json / transcript) が DB に残らない
status: 設計待ち
priority: 中
opened: 2026-10-18
closed: null
related: [improve-objective-filtered-signal-quality, improve-backlog-self-feedback-duplicates-work, mission-output-not-persisted]
kind: 不具合
---
# [improve-mission-output-not-persisted] 改善 mission の出力 (output_json / transcript) が DB に残らない

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

improvement_runs 69 件に対して missions.output_json は 0 件、transcript_json は空配列。直近の mission が「何を試したか」を事後に調べる材料が無く、backtest_runs / candidate_archives / backlog の note / 承認 payload から復元するしかない (2026-10-18 の改善ループの質の現状調査で判明)。仕様 (保存しない) か未配線かを切り分け、効果測定と事後調査のために最終出力と要約を残す。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-18: 起票。
- 2026-10-23: 関連: [improve-objective-filtered-signal-quality, improve-backlog-self-feedback-duplicates-work, mission-output-not-persisted] — 重複チケット mission-output-not-persisted を本件に統合 (2026-10-23 棚卸し)
- 2026-10-25: 種別: 不具合 — 種別を付与
- 2026-11-08: 状態: 実装中 — output_json の配線を improve-run-summary-for-humans の束で行う (transcript は保存しない、別途)
- 2026-11-08: 状態: 設計待ち — output の側は improve-run-summary-for-humans の束で是正: missions.output_json に agent の元の出力を保存する (文字列の葉と key に safe_text、秘密名の欄は伏せる、64 KiB 超は truncated の印、出力の無い経路は NULL)。残りは transcript だけ。transcript は秘密を含みうるため保存していない (今のまま []) — 保存するなら置き場・無害化・保持期間を設計する
