---
id: rollback-foreign-temp-link-sweep
title: foreign で終端した rollback の temp symlink を sweep の対象にする
status: 設計待ち
priority: 低
opened: 2026-10-18
closed: null
related: [deployed-plugin-has-no-disable-or-rollback]
kind: 不具合
---
# [rollback-foreign-temp-link-sweep] foreign で終端した rollback の temp symlink を sweep の対象にする

**状態**: 設計待ち / **優先**: 低 / **種別**: 不具合

## 現象

rollback が temp symlink (.<name>.link-d<op_id>) を作った後に第三者の live 変更で foreign 終端すると、temp は swap を通らないので残る。無害 (参照されない) だが、switch の sweep ④ は承認済み版を指す temp を消さない。sweep の対象化か、reconcile の foreign 終端時に unlink するかを決める。spec v1.1 §14 の後続。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-18: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与
