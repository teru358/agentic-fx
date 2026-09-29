---
id: selftest-loop-no-cutoff
status: 設計待ち
priority: 未設定
opened: 2026-09-07
closed: null
related: []
backfilled: true
source_section: 未完了
---
# [selftest-loop-no-cutoff]

**状態**: 設計待ち / **優先**: 未設定

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[selftest-loop-no-cutoff] 設計承認 2026-09-07** (ユーザー裁定: ① Tier F 分離起票 / ② Tier B「1 回目の self-test は許す」/ ③ codex backend で Tier F が効かない残存を許容)。設計 v4 `tmp/design-selftest-cutoff/design.md` (codex 3 周、`codex-design-review{,-2,-3}.md`)。前提の実測: opencode transcript 15 本は二峰性 — 成功 write ≤ 11 / self-test ≤ 9 / backtest 2〜3、失敗 4 本 write 24〜53 / self-test 22〜51 / **backtest 0 で例外なし**。`run_backtest` は self-test 合否を見ないのにモデルは「通るまで backtest 不可」と信じていた。本チケット = Tier A (同一失敗署名 3 回で警告、署名は node id + 例外型 + assert 式、実値除外、警告のみ) / B (strategy は成功 backtest 0 のうち self-test 2 回目以降を拒否、mission 全体で backtest 前 self-test 3 回まで) / C (予算 `improve.tool_budget`: self-test 12 / write 30 / backtest 候補あたり 6 / 総呼び出し 100 はカウントのみ) / D (timeout/max_turns 時に親が固定 idea の system note を Tx 内 best-effort upsert) / E (prompt + example コメント)。フィクスチャ `tests/fixtures/selftest_loop/` (#60 28 本 / #58 4 本、実 transcript) → **実装 `e8aa28f` (codex、段 0 変異 20/20 red) → 1 周目: codex Important 3 + Minor 1 `4c6e27e` / ローカル 3 本 Y 6 `8721b6a` (`tmp/review-20260907-st/verified-round1-*.md`) → 2 周目: codex `49a6bd5` (二重 API 廃止・片側配線 fail closed・共有テスト公開挙動化) / `/code-review high` 8 件中採用 5 `75b3663` (**本番欠陥 1: 収集エラー経路の failed_tests 文字分割**、timeout と収集エラーの署名分離、OSError を届いた失敗に) / ローカル 3 本 Y 1 `f637fb6` (`tmp/review-20260907-st-r2/verified-round2-*.md`)。フルスイート 3447 passed (既知 flake 2 群)。3 周目要否はユーザー判断待ち (2 周目で本番欠陥 1 件)。次: A4 6 回目で Tier B/C の実機確認**

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。

