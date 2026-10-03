---
id: app-state-write-has-no-lock-latch-can-be-lost
title: kill switch の状態ファイルの書き込みに lock が無い
status: 是正済
priority: 高
opened: 2026-10-03
closed: 2026-10-03
related: []
---
# [app-state-write-has-no-lock-latch-can-be-lost] kill switch の状態ファイルの書き込みに lock が無い

**状態**: 是正済 / **優先**: 高

## 現象

StateStore.update は lock なしの read-modify-write で、一時ファイル名も固定 (app_state.tmp、store/state.py:75-83)。対話シェルの killswitch reset と executor のラッチ (trade_loop.py:346 の core_lock 区間) が重なると、ラッチが消えるか、壊れた JSON で次の起動が止まり得る。daemon に操作 API を載せると daemon でも同じ窓が開く。

## 原因

## 処置案・裁定

StateStore にプロセス内 lock と一意の一時名を足す。解除は「ラッチ中のときだけ」の比較更新にする。操作 API の T0 として実装 (tmp/design-ops-api/C0.md §6)。

## 修正内容

- 2026-10-03: StateStore の更新をプロセス内 lock + flock で排他 (読者も共有 lock)。一意の一時名 + fsync + replace + 親 dir fsync。kill_switch_generation / latched_at を追加し、解除は status で見た世代の指定 (killswitch reset <世代>) のみ。解除は write-ahead の marker で、途中失敗は別プロセス・再起動後もラッチ中として読まれ、killswitch reconcile confirm でラッチ中として確定する手順のみ。lock が取れないときは fail closed。段 0 変異 60 本、codex sol 4 周 + /code-review high。main 投入 2026-10-03。

## 経緯

- 2026-10-03: 起票。
- 2026-10-03: 状態: 実装中 — 2026-10-03 着手 (並行実装、Landlock 設計の収束待ちの間)。
- 2026-10-03: 是正内容 — StateStore の更新をプロセス内 lock + flock で排他 (読者も共有 lock)。一意の一時名 + fsync + replace + 親 dir fsync。kill_switch_generation / latched_at を追加し、解除は status で見た世代の指定 (killswitch reset <世代>) のみ。解除は write-ahead の marker で、途中失敗は別プロセス・再起動後もラッチ中として読まれ、killswitch reconcile confirm でラッチ中として確定する手順のみ。lock が取れないときは fail closed。段 0 変異 60 本、codex sol 4 周 + /code-review high。main 投入 2026-10-03。
