---
id: ctl-improve-repeat-duration-and-oneoff-comment
title: afx ctl improve に繰り返し実施期間 (-t) と一時的な指示 (-c) を足す
status: 設計待ち
priority: 中
kind: 機能
opened: 2026-10-25
closed: null
related: [improve-feedback-structure, improve-run-summary-for-humans, improve-start-and-result-to-discord]
---
# [ctl-improve-repeat-duration-and-oneoff-comment] afx ctl improve に繰り返し実施期間 (-t) と一時的な指示 (-c) を足す

**状態**: 設計待ち / **優先**: 中 / **種別**: 機能

## 現象

1 回の戦略改善でうまくいくことはなく、数をこなすしかない。いまは afx ctl improve を 1 回ずつ手で打つ。(1) 期間指定: -t 1h のようにタイマーをかけ、時間が残っている限り improve を再試行させたい。残り数分で走り始めても作業を切り捨てるのは勿体ないので完走を許容する (最低保証: -t 1m でも 1 回は実施)。繰り返しの間、連続した情報 (族別の試行結果、直前の mission の観察文) は保持できるほうがよい。(2) 一時的な指示: backlog や policy/directives.md は恒久的な指示しかできず「今回だけ」の融通が難しい。-c 'ほげほげ' のような comment で、その繰り返し期間 (または 1 回) に限る指示を mission のプロンプトに注入したい (2026-10-25 ユーザー依頼)。

## 原因

## 処置案・裁定

設計で決める点: (a) 繰り返しの単位は job 1 本 (期限つき) で mission を直列に回す。daemon 再起動で job は失われてよいか。(b) 期間中の情報の保持は改善 feedback (族別試行結果、2026-10-24 承認) に乗せ、comment は同一 job 内の全 mission に同じ文で注入する (directives.md と同じ遮断 8 の除染、長さ上限、activity と Discord 通知には固定語だけ)。(c) 既定: -t なし = 従来の 1 回。週次は変えない (周回数でなく情報構造を変える方針 2026-10-24 と矛盾しないよう、手動の繰り返しは利用者の明示操作に限る)。(d) CLI は afx ctl improve のオプション追加 (cli_surface の record と golden、ops-api の job 契約)。ウィザード後の優先度はユーザー裁定。

## 修正内容

## 経緯

- 2026-10-25: 起票。
