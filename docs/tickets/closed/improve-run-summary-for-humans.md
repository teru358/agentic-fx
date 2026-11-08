---
id: improve-run-summary-for-humans
title: 改善周ごとの要約 (仮説・使った指標・床との差) を人間に見せる
status: 是正済
priority: 高
opened: 2026-10-25
closed: 2026-11-08
related: [improve-objective-filtered-signal-quality, bot-event-notices-are-raw-log-lines, improve-mission-output-not-persisted]
---
# [improve-run-summary-for-humans] 改善周ごとの要約 (仮説・使った指標・床との差) を人間に見せる

**状態**: 是正済 / **優先**: 高 / **種別**: 機能

## 現象

改善ループは動いて候補を作っているが、提出床 (PF ≥ 1.0) 未達の試行は backlog の observation / note 行にしか残らず、承認待ちにも plugin にも現れない。利用者は「改善が行われているか」を読み取れない (2026-10-25、手動 improve 5 回で候補 5 本すべて床未達、承認 0)。案: 改善 mission 1 回ごとに「試した仮説 / 使った指標 (新設した indicator を含む) / 床との差 (PF・取引数など) / 次回への申し送り」を 1 ブロックに要約し、通知 (notify) と同梱 web の 1 ページに出す。backlog の note は現状維持。要約文は改善 mission の出力から決定論的に組み立て、LLM の自由記述は引用に限る。

## 原因

## 処置案・裁定

## 修正内容

- 2026-11-08: 改善 mission の終端ごとに親が要約を組み立てて improve_run_summaries に保存する (起動時の回収を含む 13 経路、tx 内で settle → SAVEPOINT の申し送り → 直列化 → CAS 後の INSERT)。項目は仮説・agent の説明 (API と web のみ)・観察文・親の判定理由・試したもの・床との差 (verdict の判定値と閾値)・配備中の版との比較・申し送り (次の mission の族別試行結果と同じ投影)。GET /v1/improve/runs と詳細の summary、afx ctl improve runs。Discord の終了通知は有効な要約から内容行を組み、出す項目は 1 か所の表 (仮説・説明は出さない、holdout は 10-25 の範囲だけ)。web の画面は web-plugin-improve-hub で作る。レビュー: 設計 3 周 + 裁定 3 点、実装 codex 1 周目・/code-review high・閉鎖確認 2 回。判定: loops 2247 / ops+store 4614 / plugin 3478 / 直下 2095 / afx-ops 4091 ほか、既知の失敗のみ

## 経緯

- 2026-10-25: 起票。
- 2026-10-29: 優先: 高、関連: [improve-objective-filtered-signal-quality, bot-event-notices-are-raw-log-lines, improve-mission-output-not-persisted] — 2026-10-29 棚卸し: 道筋 2 番目「承認の中身を見せる」の核: 床未達の試行が承認待ちにも plugin にも現れず、改善が行われているか読めない。優先を低から引き上げ提案
- 2026-11-08: 状態: 実装中 — 設計 C2.2 (docs/superpowers/specs/2026-11-08-improve-run-summary-design.md) を利用者が承認し着手した。裁定: 仮説・説明は API と web のみ、申し送りは親の集計、output_json は同じ束
- 2026-11-08: 是正内容 — 改善 mission の終端ごとに親が要約を組み立てて improve_run_summaries に保存する (起動時の回収を含む 13 経路、tx 内で settle → SAVEPOINT の申し送り → 直列化 → CAS 後の INSERT)。項目は仮説・agent の説明 (API と web のみ)・観察文・親の判定理由・試したもの・床との差 (verdict の判定値と閾値)・配備中の版との比較・申し送り (次の mission の族別試行結果と同じ投影)。GET /v1/improve/runs と詳細の summary、afx ctl improve runs。Discord の終了通知は有効な要約から内容行を組み、出す項目は 1 か所の表 (仮説・説明は出さない、holdout は 10-25 の範囲だけ)。web の画面は web-plugin-improve-hub で作る。レビュー: 設計 3 周 + 裁定 3 点、実装 codex 1 周目・/code-review high・閉鎖確認 2 回。判定: loops 2247 / ops+store 4614 / plugin 3478 / 直下 2095 / afx-ops 4091 ほか、既知の失敗のみ
