---
id: improve-start-and-result-to-discord
title: improve の開始と結果を Discord に通知する
status: 設計待ち
priority: 中
opened: 2026-10-25
closed: null
related: [improve-run-summary-for-humans, bot-event-notices-are-raw-log-lines]
---
# [improve-start-and-result-to-discord] improve の開始と結果を Discord に通知する

**状態**: 設計待ち / **優先**: 中

## 現象

`afx ctl improve` や週次の改善 mission が始まったこと、終わったこと (候補を提出した / 床未達で見送った / 失敗した) が Discord (ops-notify / ops-bot) に出ない。利用者は backlog の note か approval list を自分で見るしかない (2026-10-25 ユーザー依頼)。案: 改善 mission の開始・終了を activity / event にし、notify がそれを「improve 開始 (job id、課題)」「improve 終了 (結果: submitted <plugin名 kind> / not_submitted (床未達) / failed (理由語)、所要時間)」の 2 通にして送る。本文は決定論的に組み立て、LLM の自由記述は引用しない。要約の中身 (仮説・指標・床との差) は improve-run-summary-for-humans が担当し、本件は開始・終了の通知だけ。

## 追加要望 (2026-10-25 ユーザー)

終了通知に次の 2 つも出す。

- **mission_observation の内容**: agent が提出をやめたときの観察文 (activity の `mission_observation` の `reason=observation:<文>` と同じもの)。agent の自己申告であることをラベルで示す (「agent の申告:」)。起票時の「LLM の自由記述は引用しない」はこの 1 項目については緩め、長さ上限 (例 300 字) と Discord の整形文字のエスケープで閉じる。
- **gate_failed の拒否理由**: 固定語 (unprofitable / insufficient_trades / sandbox 違反の種別) に加え、親専有レポート (`data/improve_reports/improve-<日付>-<mission>.md`) の `failed_stage` と、落ちた段の pair 別 pf / avg_r / 取引数 / evaluable を人間向けに要約する (例: `holdout: USDJPY pf 1.85 avg_r +0.30 取引 13 (evaluable=false)`)。この数値は人間だけに届く経路 (Discord) に限り、backlog の `last_result` や次回 mission のプロンプトには流さない (遮断 8 を維持)。レポート file が無い mission_failed は理由語のみ。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-25: 起票。
- 2026-10-25: ユーザー要望を追加 (mission_observation の文と gate_failed の拒否理由・落ちた段の数値を通知に出す)。
