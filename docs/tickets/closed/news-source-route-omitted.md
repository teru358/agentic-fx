---
id: news-source-route-omitted
title: 改善ループ経由のニュースソース追加経路の廃止 (反映済)
status: 是正済
priority: 未設定
opened: 2026-09-07
closed: 2026-10-02
related: []
backfilled: true
source_section: 未完了
---
# [news-source-route-omitted]

**状態**: 是正済 / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[news-source-route-omitted] 裁定 2026-09-07 (ユーザー承認)**: 改善ループ経由のニュースソース追加ルートを廃止。実態: 設計書 §6 にあるだけで未実装 (artifact は plugin/report/observation のみ、approval kind=news_source の生成経路なし、実 DB の approval は plugin 5 件)。理由: plugin にはバックテストという定量ゲートがあるがニュースソースには評価軸が無く、LLM でも人間でも「質を上げるか」は事前に判断できない。整理: ① 設計書 §6 の出力経路を plugins/ + 提案レポートの 2 本に ② ソース追加・無効化は人間の運用操作 (必要時に `news add` コマンド、store は既存) ③ LLM の「欲しい」は report 形 `proposal_kind: research` に吸収 ④ `approval_requests.kind` の `news_source` 列挙をコメント/docstring から落とす (DB 制約無し、migration 不要)。A4 6 回目の結果後に一括で文言修正 (テスト影響なし)

## 修正内容

- 2026-10-02: 仕分け (2026-10-02) で是正済と確認。2026-09-07 裁定で経路を廃止し、コードに news_source 列挙は残っていない。設計書 §6 の文言は未確認。 根拠: src に approval kind=news_source の生成経路や列挙は無い (grep で news_sources store 参照のみ)。

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 題名: 改善ループ経由のニュースソース追加経路の廃止 (反映済) — 仕分け (2026-10-02): 題名を付与
- 2026-10-02: 是正内容 — 仕分け (2026-10-02) で是正済と確認。2026-09-07 裁定で経路を廃止し、コードに news_source 列挙は残っていない。設計書 §6 の文言は未確認。 根拠: src に approval kind=news_source の生成経路や列挙は無い (grep で news_sources store 参照のみ)。
