---
id: ctl-improve-repeat-duration-and-oneoff-comment
title: afx ctl improve に繰り返し実施期間 (-t) と一時的な指示 (-c) を足す
status: 是正済
priority: 中
kind: 機能
opened: 2026-10-25
closed: 2026-10-28
related: [improve-feedback-structure, improve-run-summary-for-humans, improve-start-and-result-to-discord]
---
# [ctl-improve-repeat-duration-and-oneoff-comment] afx ctl improve に繰り返し実施期間 (-t) と一時的な指示 (-c) を足す

**状態**: 是正済 / **優先**: 中 / **種別**: 機能

## 現象

1 回の戦略改善でうまくいくことはなく、数をこなすしかない。いまは afx ctl improve を 1 回ずつ手で打つ。(1) 期間指定: -t 1h のようにタイマーをかけ、時間が残っている限り improve を再試行させたい。残り数分で走り始めても作業を切り捨てるのは勿体ないので完走を許容する (最低保証: -t 1m でも 1 回は実施)。繰り返しの間、連続した情報 (族別の試行結果、直前の mission の観察文) は保持できるほうがよい。(2) 一時的な指示: backlog や policy/directives.md は恒久的な指示しかできず「今回だけ」の融通が難しい。-c 'ほげほげ' のような comment で、その繰り返し期間 (または 1 回) に限る指示を mission のプロンプトに注入したい (2026-10-25 ユーザー依頼)。

## 裁定と案 (2026-10-25)

- 時間は h と m で指定する (例 `1h30m`、`45m`、`2h`)。単位なしの数は拒否 (rc=2)。
- 最長期間は設ける。指揮者推奨 **24h** (超過は rc=2)。理由: 無人で codex / claude の枠を食い尽くす、429 連続で `mission_failed` が並ぶ、週次と重なる。settings で変えられる値にはしない (安全側の固定)。
- オプション案: `-t/--duration <h/m>` (繰り返し期間)、`-c/--comment '<文>'` (その job 限りの指示。上限 1000 字、directives.md と同じ除染、activity と Discord には固定語だけ)、`-n/--max-runs N` (回数でも止められる。`-t` と併用時は先に達したほうで止める)。既定は従来の 1 回。
- 細かい設計は実装時に決める (ユーザー了承)。

## 原因

## 処置案・裁定

設計で決める点: (a) 繰り返しの単位は job 1 本 (期限つき) で mission を直列に回す。daemon 再起動で job は失われてよいか。(b) 期間中の情報の保持は改善 feedback (族別試行結果、2026-10-24 承認) に乗せ、comment は同一 job 内の全 mission に同じ文で注入する (directives.md と同じ遮断 8 の除染、長さ上限、activity と Discord 通知には固定語だけ)。(c) 既定: -t なし = 従来の 1 回。週次は変えない (周回数でなく情報構造を変える方針 2026-10-24 と矛盾しないよう、手動の繰り返しは利用者の明示操作に限る)。(d) CLI は afx ctl improve のオプション追加 (cli_surface の record と golden、ops-api の job 契約)。ウィザード後の優先度はユーザー裁定。

## 修正内容

- 2026-10-28: afx ctl improve に -t / -n / -c を実装 (設計 v0.5.3、T1 controller + T2 面、main 73428607、配備 2026-10-28 19:08)。本機で -t 30m -c を実行: 受理 rc=0、5 回実施、期限で stop_reason=deadline、期限直前に始めた mission は打ち切らず完走、一時指示の本文は activity / 技術ログ / DB に残らないことを grep で確認。

## 経緯

- 2026-10-25: 起票。
- 2026-10-25: 時間単位 (h/m)、最長期間 24h、オプション案 (-t / -c / -n) を追記。
- 2026-10-26: 状態: 実装中 — 2026-10-26 設計 C0 v0.4 閉鎖 (sol r1 17 → v0.2 → sol r2 12 → v0.3 → sol r3 差分 3 → v0.4、指揮者の差分検収で閉鎖)。§8 の 6 件はユーザー裁定済。実装 T1 着手
- 2026-10-28: 是正内容 — afx ctl improve に -t / -n / -c を実装 (設計 v0.5.3、T1 controller + T2 面、main 73428607、配備 2026-10-28 19:08)。本機で -t 30m -c を実行: 受理 rc=0、5 回実施、期限で stop_reason=deadline、期限直前に始めた mission は打ち切らず完走、一時指示の本文は activity / 技術ログ / DB に残らないことを grep で確認。
