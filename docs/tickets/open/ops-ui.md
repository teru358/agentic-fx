---
id: ops-ui
title: 承認 bot と読み取り専用 web ダッシュボードが無い
status: 設計待ち
priority: 低
opened: 2026-09-19
closed: null
related: [first-run-setup, first-run-setup]
backfilled: true
source_section: 未完了
---
# [ops-ui]

**状態**: 設計待ち / **優先**: 低

## 現象・原因・処置案 (tickets.md からの移行、原文)

**[ops-ui] 方針 2026-09-19 (ユーザー)**: 操作 API (設計書 §7) + Discord 承認 bot + 読み取り専用 web ダッシュボードの束。**配布形では汎用 bot (`~/project/discord_bot`、別リポジトリ) が付属しないので、`mt5_bridge/` と同じ流儀の最小同梱パッケージ (リポジトリ直下の独立 dir + 自前 pyproject/uv.lock/README/tests、本体とは §7 の HTTP API だけで結合) として単機能の承認 bot を用意する**。この環境は既存の汎用 bot + `cogs/agentic_fx/` のままでよい (両者は同じ API のクライアント、機能は同梱版が下限)。設計書 §9「別リポジトリの cog」を「同梱の最小 bot を正、汎用 bot の cog は任意」に改訂。通知は従来どおり本体の webhook 直送 (bot 不要)。web に書き込み (指示・承認) を載せるかは設計時に伺う (§16「汎用 REST API は作らない」との線引き、`autopilot on` は API に載せない)。**着手はペーパー到達の後**、[first-run-setup] のウィザードに bot token / 承認者ロールの設定を含める **裁定 2026-09-19 (ユーザー承認): 付属型 (別プロセスの同梱パッケージ) で進める。内蔵型は採らない** — 根拠: ①故障分離 (discord.py の event loop・再接続・依存の例外を SL 監視と同じプロセスに載せない、bot 停止時も CLI で承認続行) ②権限境界が API 契約で構造的に決まる (approver キーのみ、`autopilot on` に届かない) ③外部入力のパースと常時 websocket を資金を扱うプロセスに持ち込まない ④bot だけ再起動できる ⑤汎用 bot の cog と実装を 1 本に保てる。内蔵型の唯一の利点 (導入の手間) は [first-run-setup] のウィザードが bot の user service 設置と API キー自動生成を行うことで解消する

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 設計待ち、優先: 低、題名: 承認 bot と読み取り専用 web ダッシュボードが無い — 仕分け (2026-10-02、現物で成立を確認): 付属型 (別プロセス) で進める裁定済み、着手はペーパー到達の後。 根拠: 同梱 bot / web は未実装。裁定 2026-09-19 で付属型に決定、着手はペーパー到達後。
