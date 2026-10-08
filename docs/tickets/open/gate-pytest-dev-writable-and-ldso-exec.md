---
id: gate-pytest-dev-writable-and-ldso-exec
title: gate の pytest は /dev 全体が書き込み可で ld.so 経由で任意の ELF を起動できる
status: 設計待ち
priority: 高
opened: 2026-10-03
closed: null
related: []
---
# [gate-pytest-dev-writable-and-ldso-exec] gate の pytest は /dev 全体が書き込み可で ld.so 経由で任意の ELF を起動できる

**状態**: 設計待ち / **優先**: 高

## 現象

gate_pytest_worker の allowlist は /dev をディレクトリ単位で書き込み可にしており、ld.so (汎用ランチャ、memory landlock-execute-semantics) の実行も許している。候補 plugin のテストコードが任意の ELF を起動できる (2026-10-03、plugin worker Landlock の設計中に確認)。

## 原因

## 処置案・裁定

/dev は必要なファイル (null、urandom) だけに絞る。ld.so の exec を許さない (interpreter_files_for の結果から ld.so を除く、または実行を python 実体のみに)。plugin worker の allowlist と同じ自己検査を gate にも掛ける。

## 修正内容

## 経緯

- 2026-10-03: 起票。
