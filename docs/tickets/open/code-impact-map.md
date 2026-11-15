---
id: code-impact-map
title: コードから読み取る現状の影響地図 (機械索引・契約表・領域頁) を docs/ に置く
status: 実装中
priority: 高
kind: 保守
opened: 2026-11-14
closed: null
related: [auto-killswitch-latch-emits-no-event, bot-gateway-disconnect-is-silent]
---
# [code-impact-map] コードから読み取る現状の影響地図 (機械索引・契約表・領域頁) を docs/ に置く

**状態**: 実装中 / **優先**: 高 / **種別**: 保守

## 現象

コードが約 12 万行に育ち、チケット単位の実装では全体への影響が見えにくい。2026-11 には guard_state_changed を一部の経路だけが出す、shell が StateStore を直接変えて ops lane を迂回する、docstring と実動作が食い違う、が続いた。基本設計は 10 月から更新が止まり、個別の設計書は変更単位の履歴で現状を表さない。現状の正本として、(1) script で作り直せる機械索引 (DB 表・event code・state file・config key ごとの書き手と読み手、daemon と afx-ops の境界)、(2) 状態遷移 × 入口 × 必要な副作用の契約表 (観測事実 / 守る契約 / 既知の逸脱)、(3) 領域ごとの頁、(4) 鮮度の管理 (最終検証 commit と依存先、要確認頁の列挙)、(5) 設計・実装・レビューの工程への組み込み、を docs/ に置く。まず kill switch と guard state の領域で試し、手で作った正解表で抽出の正確さを測ってから全域へ広げる。契約が変わるチケットは同じ commit で該当頁を直す。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-11-14: 起票。
- 2026-11-14: 状態: 実装中 — 設計 C2.1 を 2026-11-14 に利用者承認 (docs/superpowers/specs/2026-11-14-code-impact-map-design.md)。範囲はチケット本文より狭めて始める: 初回 (T1) は試行頁 1 枚 + 独立の正解表 A/B + literal 逆引きの extract/evaluate + test 約 6 本。合否は抽出規則の種類 × 領域で出し、未測定の種類は unsupported。鮮度判定・affected・check (本文 (4)) は T2、工程への組み込み (本文 (5)) は T3 の実運用確認の後。正式試行の基準 revision は opsalert の merge 後。T0 (頁の雛形と手順) から着手。
- 2026-11-15: 状態: 実装中 — T1 (初回) を完了: kill switch 頁 docs/impact-map/domains/kill-switch.md、独立正解表 A・B と凍結表 docs/impact-map/oracle/、scripts/impact_map.py (extract / evaluate、標準ライブラリだけ) と tests/scripts/test_impact_map.py。初回要件 (機械セル critical 81/81・頁セル critical 208/208・FN=0・U=0、非 critical FN の全件列挙、FP の件数と仕分け時間、C4 前 558cb9fd の履歴再現 a/b/c) を満たした。経過 (抽出器の是正・頁の手動補完・正解表の追加訂正 3 回) は docs/impact-map/evaluations/kill-switch.md。次は T2 (鮮度・affected・check、非 critical の頁補完、正解表見直し)。
- 2026-11-15: 状態: 実装中 — T2 (照合記録 verify・check・affected・副 anchor・固有例外台帳と直呼び検査・非 critical の頁補完) を閉鎖。レビューは codex 1 周目 (Important 6・Minor 1)、閉鎖確認 3 回、/code-review high (10 件中 7 採用・3 却下)、閉鎖確認 2 回。次は T3 (C4 の遡及運用確認と HWM の運用確認)
