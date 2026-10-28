---
id: selftest-failure-detail-not-logged
title: improve 隔離の自己試験が落ちた理由がどこにも残らない
status: 是正済
priority: 中
kind: 不具合
opened: 2026-10-27
closed: 2026-10-28
related: [improve-gpt6-models-fail-isolated-selftest]
---
# [selftest-failure-detail-not-logged] improve 隔離の自己試験が落ちた理由がどこにも残らない

**状態**: 是正済 / **優先**: 中 / **種別**: 不具合

## 現象

起動時の improve 隔離 admission で自己試験が不合格になると、技術ログには `admitted=no reason=runtime_fingerprint_selftest_failed` だけが出る。不合格の条件 (echo 未到達、MCP の initialize / tools/list / list_staging 呼び出しの欠落、grace 超過、CLI 子プロセス未観測、子プロセス未回収など、loops/verify_backend.py が組む detail) は ProbeOutcome.detail (core/improve_canary.py の run_selftest) に入るが、runtime_fingerprint の admission で捨てられ、ログにも activity にも残らない。失敗の理由が読めないので原因を絞れない (2026-10-27、gpt-6-luna で再現)。是正案: 不合格時に detail (固定語の組立てで 200 字上限) を技術ログに 1 行出す。秘密や自由文が入らないことを確認する。

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-28: 自己試験の不合格を構造化した項目から allowlist 形式の detail (gate / status / 固定語の reason / watch 集計 / 未回収件数 / refusal / blocking) に 1 か所で導き、新しい不合格判定ごとに技術ログへ `improve isolation: selftest detail: …` を 1 行出す (cache 返却では出さない、既存の admission 行はバイト不変)。一時失敗の判定は sanitize 前の status と運用者向け診断で行う。afx check backend は運用者の shell にだけ自由文を出す。main de65d1e8、配備 2026-10-28。レビュー codex 3 周 + /code-review high。本番での不合格行の実観測は未 (次に自己試験が落ちたとき)。

## 経緯

- 2026-10-27: 起票。
- 2026-10-28: 是正内容 — 自己試験の不合格を構造化した項目から allowlist 形式の detail (gate / status / 固定語の reason / watch 集計 / 未回収件数 / refusal / blocking) に 1 か所で導き、新しい不合格判定ごとに技術ログへ `improve isolation: selftest detail: …` を 1 行出す (cache 返却では出さない、既存の admission 行はバイト不変)。一時失敗の判定は sanitize 前の status と運用者向け診断で行う。afx check backend は運用者の shell にだけ自由文を出す。main de65d1e8、配備 2026-10-28。レビュー codex 3 周 + /code-review high。本番での不合格行の実観測は未 (次に自己試験が落ちたとき)。
