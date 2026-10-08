---
id: legacy-e2e-econ-429
title: 再起動のたびに経済指標を取得し連打で 429 になる
status: 是正済
priority: 中
opened: null
closed: 2026-10-02
related: []
backfilled: true
source_section: 未完了
---
# [legacy-e2e-econ-429]

**状態**: 是正済 / **優先**: 中

## 現象・原因・処置案 (tickets.md からの移行、原文)

[E2E-econ-429] サービス起動毎に econ fetch が走り、再起動連打で HTTP 429 (実測)。起動時 fetch のレート制御/クールダウンが無い — outbound-request-budget 案件

## 修正内容

- 2026-10-02: main fd7b717・6ae25ea。経済指標とニュースの取得は、最後に試した時刻を alert_state に保存し、再起動をまたいで定期間隔 (ニュース 30 分、経済指標 6 時間) を守る。失敗後も次の定期時刻まで待ち、429/503 の Retry-After があれば長いほうまで待つ (24 時間で頭打ち)。記録の無い初回起動は従来どおり取得。保存値が未来・破損なら間隔 1 回分待つ。レビュー: codex terra 1 周 (Important 2・Minor 1 を是正または受容)。フルスイート 5045 passed (既知 flake 1)。実機受入は次回の再起動で econ_fetch_skipped / news_fetch_skipped が出ること

## 経緯

- 2026-09-29: `.superpowers/sdd/plan10-plan/tickets.md` から機械移行 (backfilled)。状態は移行時の判定。
- 2026-10-02: 状態: 実装待ち、優先: 中、題名: 再起動のたびに経済指標を取得し連打で 429 になる — 仕分け (2026-10-02、現物で成立を確認): 起動時 fetch のクールダウンが無く、外向きリクエスト予算の観点で要対策。 根拠: core/scheduler.py:323-325 で _last_econ が None の起動直後に必ず econ.refresh が走り、_last_econ はメモリのみで再起動連打を抑えない。
- 2026-10-02: 是正内容 — main fd7b717・6ae25ea。経済指標とニュースの取得は、最後に試した時刻を alert_state に保存し、再起動をまたいで定期間隔 (ニュース 30 分、経済指標 6 時間) を守る。失敗後も次の定期時刻まで待ち、429/503 の Retry-After があれば長いほうまで待つ (24 時間で頭打ち)。記録の無い初回起動は従来どおり取得。保存値が未来・破損なら間隔 1 回分待つ。レビュー: codex terra 1 周 (Important 2・Minor 1 を是正または受容)。フルスイート 5045 passed (既知 flake 1)。実機受入は次回の再起動で econ_fetch_skipped / news_fetch_skipped が出ること
