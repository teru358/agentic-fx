---
id: wizard-stage12-show-examples
title: 段 12 の方針文と最初の課題に例文と書き方の案内を出す
status: 是正済
priority: 中
opened: 2026-10-14
closed: 2026-10-16
related: [first-run-setup, wizard-stage9-summary-in-user-terms]
---
# [wizard-stage12-show-examples] 段 12 の方針文と最初の課題に例文と書き方の案内を出す

**状態**: 是正済 / **優先**: 中

## 現象

段 12 で「方針文」「最初の課題」を聞かれても、初めての利用者は何を書けばよいか分からない (2026-10-14 stream-svr 受入 1 回目のユーザー指摘)。問いの前に (1) 何に使われるか 1 行 (方針文は取引判断と改善の全 mission に注入される運用者の方針、課題は改善ループが最初に取り組む 1 件)、(2) 例文 2〜3 行 (方針: ペーパー運用・対象ペアと足・入らない時間帯・「選別して勝つ」を優先・提案は 1 つの考えに絞る。課題: 標準指標だけで組める 1h の押し目買い戦略候補を 1 本作り直近 3 ヶ月で PF と最大 DD を測る)、(3) 「後から afx ctl の policy add / improve add で足せる、空 Enter で飛ばせる」を表示する。例文は e で入力欄に流し込めるとよい。directives.md の実文言はユーザーが考える方針 (2026-09-12) は変えない: 例はあくまで雛形

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-16: 段 12 は用途 1 行 + 例文 (ペアと判断足を埋める) + 後から足せる旨、e で例文を入力 (重複追記なし)。main 42a1ded (fr-fix2、spec v1.8。codex r1 → /code-review high r2 → codex r3、是正 3 周)

## 経緯

- 2026-10-14: 起票。
- 2026-10-15: 状態: 実装中 — 束 fr-fix2 (spec v1.8、ブリーフ tmp/review-20261011-sc/fr-fix2-1.md / fr-fix2-2.md)
- 2026-10-16: 是正内容 — 段 12 は用途 1 行 + 例文 (ペアと判断足を埋める) + 後から足せる旨、e で例文を入力 (重複追記なし)。main 42a1ded (fr-fix2、spec v1.8。codex r1 → /code-review high r2 → codex r3、是正 3 周)
