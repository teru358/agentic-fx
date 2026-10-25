---
id: deploy-cli-config-error-traceback
title: afx の subcommand は設定不在・不正 YAML で traceback を出す
status: 設計待ち
priority: 中
opened: 2026-10-18
closed: null
related: [deployed-plugin-has-no-disable-or-rollback, cli-surface-principle-and-reorg]
kind: 不具合
---
# [deploy-cli-config-error-traceback] afx の subcommand は設定不在・不正 YAML で traceback を出す

**状態**: 設計待ち / **優先**: 中 / **種別**: 不具合

## 現象

config/settings.yaml が無い、または YAML が壊れていると、afx plugin versions 以外の afx subcommand (history / backtest / plugin submit など) は ConfigError が例外境界の外で起きて traceback を出す。versions だけは T7 で code=config_error の 1 行に直した。他も同じ形 (rc 1、code 付き 1 行、何も作らない) に揃える。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-18: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与
