---
id: improve-admission-hashes-cli-every-mission
title: 改善 mission の admission が毎回 CLI binary を hash し fingerprint を再計算する
status: 設計待ち
priority: 低
opened: 2026-10-11
closed: null
related: []
kind: 保守
---
# [improve-admission-hashes-cli-every-mission] 改善 mission の admission が毎回 CLI binary を hash し fingerprint を再計算する

**状態**: 設計待ち / **優先**: 低 / **種別**: 保守

## 現象

admit() は mission のたびに inspect_artifact で数十〜数百 MB の CLI binary を読んで hash し、compute_fingerprint で source 群の再読込と AST 解析、BPF 生成、outer_landlock_sources の再構築を行う (判定 cache は入口の計算を省けない)。さらに worker が sealed_artifact_copy でもう 1 回読んで hash する。改善 mission は週次なので今は許容。(dev, ino, size, mtime) が同じなら hash を使い回す等の短絡を、安全性 (TOCTOU) を損なわない形で設計する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-11: 起票。
- 2026-10-25: 種別: 保守 — 種別を付与
