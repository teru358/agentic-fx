---
id: indicator-symbol-agnostic-and-duplicate-gate
title: indicator の銘柄非依存と重複を決定論 gate で検査する
status: 是正済
priority: 中
kind: 機能
opened: 2026-10-31
closed: 2026-11-01
related: [multi-input-indicator-relations, reference-symbols-as-indicator-inputs]
---
# [indicator-symbol-agnostic-and-duplicate-gate] indicator の銘柄非依存と重複を決定論 gate で検査する

**状態**: 是正済 / **優先**: 中 / **種別**: 機能

## 現象

改善ループが作る indicator は特定銘柄に向く傾向がある (名前に銘柄、pip_size や価格差の閾値を param に持つ、JPY スケールの既定値)。契約上 indicator は銘柄を持たないが、中身がスケール依存だと別銘柄に束ねたとき黙って壊れる。また既存 indicator と同等のものが繰り返し作られる。指示 (directives) では抑えきれないので決定論の gate にする (2026-10-31 ユーザー方針)。設計方針: (1) 名前・param 名・docstring に pairs + watch_symbols の銘柄名が入れば reject (固定語)、(2) スケールの違う 2 銘柄分の canonical fixture で実行し両方で有限・非定数の出力が出ること、価格を k 倍した系列で出力が不変 or k 倍のどちらにも収まらないものは scale_dependent warning (承認画面に表示、reject はしない)、(3) param に価格単位 (pips / pip_size / price_*) を要求しない、(4) 重複: 既存 indicator と canonical fixture 上で出力が一致または相関 0.99 超なら indicator_duplicate で提出不可、inventory に「既存 X と同等」を残す (strategy の族に相当する仕組みを indicator に足す)、(5) 改善 prompt に規則を 1 文。多入力 indicator より先に入れる。

## 原因

## 処置案・裁定

設計 C0 v0.6 (docs/superpowers/specs/2026-10-31-indicator-agnostic-duplicate-gate-design.md) で清書済み。sol / opus / terra の 3 周、実測 6 便 (配備済み 56 本) を §0.2 に記録。

- 裁定: J1 grandfather (既存 indicator は遡及 reject しない) / J4 既定 warn / J5 bless でも exact 重複は reject / J6 approve 時に再判定。
- 変更: 『相関 0.99 超で reject』は実測で否定。重複は exact 一致 + 単調変換 (Spearman 2 条件) に変更し、mode warn | enforce で扱う。
- 検査の分離: 契約 run (本番形 tail(max_bars)) と解析 probe (N=4096、k 倍・volume 倍) を分ける。名前・param は静的検査。
- 人間回廊: submit = enforce / bless = warn。

## 修正内容

- 2026-11-01: T1〜T4 を実装し 223948aa で main に取り込み配備 (settings に improve.indicator_duplicate_mode: warn / indicator_similar_corr 0.98 / indicator_fixture_wall_clock_max_sec 60 を明示、relation 列 migration、afx 再起動)。名前・param 検査、契約 run と解析 probe、親専有 fixture cache (56 本 初回 31.5 秒 / 2 回目 0.39 秒)、exact 重複 reject・単調変換 warn・類似 warning、approve 時の再判定、承認詳細 4 面の表示。レビュー: 各 task で codex 1 周目 → /code-review high → terra 閉鎖

## 経緯

- 2026-10-31: 起票。
- 2026-10-31: 状態: 設計待ち — 2026-10-31 ユーザー裁定で起票。C0 は baseline T4 / ref-literal の閉鎖後、multi-input より先
- 2026-10-31: 状態: 実装待ち — 2026-10-31: 設計 C0 v0.6 (docs/superpowers/specs/2026-10-31-indicator-agnostic-duplicate-gate-design.md)。sol / opus / terra の 3 周、実測 6 便。ユーザー裁定 J1 grandfather / J4 既定 warn / J5 bless でも exact は reject / J6 approve 時再判定。チケット (4) の『相関 0.99 超で reject』は実測で否定され exact + 単調変換 (Spearman 2 条件) に変更
- 2026-11-01: 是正内容 — T1〜T4 を実装し 223948aa で main に取り込み配備 (settings に improve.indicator_duplicate_mode: warn / indicator_similar_corr 0.98 / indicator_fixture_wall_clock_max_sec 60 を明示、relation 列 migration、afx 再起動)。名前・param 検査、契約 run と解析 probe、親専有 fixture cache (56 本 初回 31.5 秒 / 2 回目 0.39 秒)、exact 重複 reject・単調変換 warn・類似 warning、approve 時の再判定、承認詳細 4 面の表示。レビュー: 各 task で codex 1 周目 → /code-review high → terra 閉鎖
