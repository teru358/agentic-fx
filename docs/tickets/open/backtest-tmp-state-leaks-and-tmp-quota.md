---
id: backtest-tmp-state-leaks-and-tmp-quota
title: backtest の一時 state が /tmp に残り続け、/tmp の枯渇で改善 mission が落ちる
status: 設計待ち
priority: 中
kind: 不具合
opened: 2026-10-25
closed: null
related: []
---
# [backtest-tmp-state-leaks-and-tmp-quota] backtest の一時 state が /tmp に残り続け、/tmp の枯渇で改善 mission が落ちる

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

2026-10-25 08:03 UTC、mission 2046 が commit_crashed:OSError:[Errno 122] Disk quota exceeded で失敗。state.py save() の fdopen で EDQUOT。/tmp は tmpfs 15G + usrquota で、同 uid の pytest basetemp (判定束、tests/core が 4 GB) が埋めていた。daemon 自体も backtest/runner.py の run_replay が tempfile.mkdtemp() に state.json を置き、後始末が無いため /tmp/tmp* が 3218 個残っていた (1 個は小さいが無期限に増える)。

## 原因

backtest の replay state を /tmp (共有・quota 付き tmpfs) に置き、削除しない。/tmp の空きは daemon の制御外なので、他プロセスの使用量が本番 mission の失敗に直結する。

## 処置案・裁定

(1) run_replay の state を TemporaryDirectory で包み終了時に消す (例外経路も)。(2) 置き場を /tmp でなく data/ 配下の作業領域 (または settings の tmp_dir) にして、quota 共有の影響を切る。(3) 空きが無いときの mission 失敗は現状どおり failed に落として記録 (fail-closed) で可。運用側: 判定束の --basetemp は終わったら消す (メモリ)。

## 修正内容

## 経緯

- 2026-10-25: 起票。
