---
id: cli-surface-principle-and-reorg
title: afx / afx ctl の置き場の原則を定義し、コマンド面を整理する
status: 実装中
priority: 中
opened: 2026-10-15
closed: null
related: [news-source-cli-and-429-backoff, deployed-plugin-has-no-disable-or-rollback]
---
# [cli-surface-principle-and-reorg] afx / afx ctl の置き場の原則を定義し、コマンド面を整理する

**状態**: 実装中 / **優先**: 中

## 現象

原則 (2026-10-15 ユーザー方針): afx ctl = 稼働中の daemon に対する操作 (操作 API 経由、認証・監査つき、bot / web と同じ操作面。daemon の生きた状態 = mission の排他スロット・承認キュー・kill switch・データ状態を読む・動かす・回復する: status/log/activity/events/jobs、approval/approve/reject/retry、ask/improve/backlog add/policy add、killswitch/data resume/reflect retry)。最上位 afx <sub> = daemon を介さない運用操作 (設定・鍵・資材を人間が整える、オフラインの検査・分析。daemon 停止中でも実行でき、稼働中は flock か次周期反映で共存: init/check/keys/ops、plugin/news/history、backtest/analyze、status)。仕分け規則「daemon の生きた状態が要るなら ctl、要らないなら最上位」。整理項目: (1) `afx improve verify-backend` を `afx check` 側へ (daemon 不要の検査。`afx improve` 群は廃止、旧名は 1 版だけ案内つきで残す)、(2) `afx ctl improve add` → `afx ctl backlog add` に改名 (別名は残す) + `backlog list`、(3) `afx ctl policy show` / `policy remove <n>`、(4) `afx plugin list` (配備済み・pending・版)、(5) オフラインの `afx status` (setup.json・未完手順・unit の有無・直近の活動を記録と DB から直接表示。daemon 停止中の診断とウィザードの起動後確認に使う。stream-svr のリセット後に afx ctl status が鍵不在で何も見せられなかった)、(6) README / 手順書 / ops-api 文書に原則の節を 1 つ。ウィザード対話の束とは分け、afx news の束の後に

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-15: 起票。
- 2026-10-17: 裁定 (ユーザー概ね承認、sol 2 回の助言を踏まえる) — 用途の分類は **7 群 × 実行レーン (最上位 `afx` = daemon 不要 / `afx ctl` = 稼働 daemon の認証・監査つき API)** の表で help と文書を組む。群: 導入・接続管理 (init / check / keys / ops | ctl whoami)、稼働観測・実行追跡 (status | ctl status / log / activity / events / jobs)、市場入力を整える (news / history)、調査・評価する (backtest / analyze)、問い合わせ・改善運用 (ctl ask / improve / backlog / policy / reflect retry)、Plugin を採否・配備する (plugin 全操作 | ctl approval / approve / reject / approval retry)、保護状態を回復する (ctl killswitch / data resume)。危険度は群名に混ぜず `[読取] [変更] [確認] [approver]` のラベル (認可の軸 status.read / operate / decide / local_guard と対応)。`ctl` は分類対象でなくレーン。indicator / strategy は分けない (`plugin list --kind` で見せる)。横断表示 (`ctl status`) は正所属 1 つで、出力末尾から次のコマンドへ誘導し重複掲載しない。help は最上位に表、`afx ctl help` は右列を同じ群順で展開、末尾に「API に発注・SL 変更・クローズ・サービス停止は無い」を 1 文。分類メタデータから help を生成し陳腐化を防ぐ。助言全文: tmp/review-20261011-sc/reports/cli-taxonomy/ (advise 1 = 既存案の評価、advise 2 = 白紙)。
- 2026-10-17: コスト見込み (指揮者) — 設計 0.5 日 (C0 短、sol 1〜2 周) + 実装 1〜1.5 日を 2 束 (A: 分類メタデータからの help 生成 + (1)(4)(5)(6)、B: (2) backlog list と (3) policy show/remove の API 追加)。影響: entry/cli/client/commands 計 3258 行、help 文言 pin の test 11 本、文書 39 か所。着手は pdo T7 (plugin disable / rollback / versions / revoke の CLI) の後。
- 2026-10-17 22:49: ユーザー裁定 — 道筋 4 番目 (pdo → 承認の中身 → 改善の質 → 本件 → ウィザード UI)。
- 2026-10-24: 状態: 実装待ち — 設計書 docs/superpowers/specs/2026-10-23-cli-surface-design.md v0.5b で収束 (設計レビュー 4 周 + 閉鎖確認 3 周)。実装順 S1 → B1 → B2a → B2b → B2c → A1 / A2 → I1、I1 は web-lan の本体 merge 後。§8.2-1 (対話 shell の寿命) はユーザー裁定待ちだが S1〜B2 を止めない
- 2026-10-24: 状態: 実装中 — S1 (cli_surface.py の分類メタデータ 59 record、prefix 木 renderer、別名と廃止名、shell lifecycle、scope guidance.read) を実装して merge。codex r1 High 1 / Medium 4、/code-review 9 件 (1 却下)、閉鎖確認 1 件を是正。全 record は I1 まで exposed=False で既存 help は不変。判定 ops+commands+backtest/cli+ops_setup/entry+setup 2237 green。次は A1 (argv shim と ops <feature> <verb>)
- 2026-10-24: 状態: 実装中 — A1 (argv shim: improve 先頭語 / ops 旧形 / reconf、ops <feature> <verb> の parser と dispatch、check backend、plugin list の view model と observer 接続、web reconfigure の入口 stub) を merge。codex r1 High 1 / Medium 6、/code-review 10 件、閉鎖確認 2 周 (opus) を是正。設計書 v0.5e。判定 acceptance+ops_setup+backtest/cli+setup+cli_surface+plugin+shim+ctl+store+commands+scripts green (tmp quota 由来の失敗は単独 green で確認)。I1 申し送り 3 (reconf 条件の record 導出、plugin list の CLI 経路 test、AC-8 の実 verify_backend)。次は B1
- 2026-10-24: 状態: 実装中 — B1 (GET /v1/backlog `guidance.read` で endpoint 26 本、store list_items の有界読み、`backlog list [--all] [--limit N]` の parse と 1 行表示、`backlog add` 正式名 + `improve add` 旧名の stderr 案内) を merge。codex r1 Medium 1、/code-review 7 件 (直す 3 / 吸収 1 / I1 送り 1 / 却下 2)、閉鎖確認 3 周 (terra、High 2: 前段切り詰めの token 分割) を是正。設計書 v0.5h。判定 ops+store+commands+cli_surface+entry_ctl+acceptance+setup 3331 green。I1 申し送り: USAGE / help に backlog list / add を載せ improve add を旧名表記にする。次は B2a
