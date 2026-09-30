---
id: worker-death-cause-observed-by-parent
title: plugin worker の死因を親プロセスで固定して読めるようにする
status: 是正済
priority: 高
opened: 2026-09-30
closed: 2026-09-30
related: [backtest-worker-cpu-budget-shrinks-with-timeframe, backtest-rpc-timeout-does-not-stop-parent-work]
---
# [worker-death-cause-observed-by-parent] plugin worker の死因を親プロセスで固定して読めるようにする

**状態**: 是正済 / **優先**: 高

## 現象

plugin worker が CPU 上限・クラッシュ・timeout のどれで死んだかを親が区別できず、改善ループや運用者が失敗の理由を読めない。stderr も捨てられていた。

## 原因

worker の終了状態を複数箇所で wait しており、close 応答の自己申告 CPU 秒に頼っていた。stdout は reader thread が読み、fd の受け渡しに競合があった。

## 処置案・裁定

spec docs/superpowers/specs/2026-09-21-backtest-failure-readable-design.md v1.3 の §2.1 / §2.2 (束 A の最初の task)。

## 修正内容

- 2026-09-30: main 1050e16..4e9a849。単一の wait4 で終了状態と rusage を 1 回だけ取り、分類表 (cpu_limit / crashed / timeout / plugin_error / protocol_error) で死因を固定。応答の読み取りは呼び出し thread の poll ループ (reader thread 廃止)、fd の close は 1 か所、kill 後 5 秒で回収できなければ unreaped として強参照保持。stderr は末尾 8 KiB を 1 行に escape して技術ログのみ。レビュー: codex sol 4 周 + terra 2 周 + 変異 60 本 (pin 11) + ローカル LLM 3 本 (採用 7) + /code-review high (採用 7) + sonnet 確認 (Critical 0)。sandbox テスト 209、フルスイート 4867 passed (既知 flake 1)。実機受入は再起動後の週次改善で確認

## 経緯

- 2026-09-30: 起票。
- 2026-09-30: 是正内容 — main 1050e16..4e9a849。単一の wait4 で終了状態と rusage を 1 回だけ取り、分類表 (cpu_limit / crashed / timeout / plugin_error / protocol_error) で死因を固定。応答の読み取りは呼び出し thread の poll ループ (reader thread 廃止)、fd の close は 1 か所、kill 後 5 秒で回収できなければ unreaped として強参照保持。stderr は末尾 8 KiB を 1 行に escape して技術ログのみ。レビュー: codex sol 4 周 + terra 2 周 + 変異 60 本 (pin 11) + ローカル LLM 3 本 (採用 7) + /code-review high (採用 7) + sonnet 確認 (Critical 0)。sandbox テスト 209、フルスイート 4867 passed (既知 flake 1)。実機受入は再起動後の週次改善で確認
