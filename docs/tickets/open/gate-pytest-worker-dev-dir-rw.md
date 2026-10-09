---
id: gate-pytest-worker-dev-dir-rw
title: gate pytest worker が /dev をディレクトリ単位 read_write で許している
status: 設計待ち
priority: 低
opened: 2026-10-09
closed: null
related: [sandbox-escape-via-user-daemons-unmeasured]
---
# [gate-pytest-worker-dev-dir-rw] gate pytest worker が /dev をディレクトリ単位 read_write で許している

**状態**: 設計待ち / **優先**: 低

## 現象

plugin/gate_pytest_worker.py の Landlock 規則は /dev 全体を read_write で許す。改善 worker 側は improve-cli-seccomp spec §5 の固定表 (/dev/null は rw、random 系は読み取りのみ、tty・ptmx・shm 等は拒否) に個別 file 規則で置き換える (task A1)。gate pytest worker は A1 の範囲外として現状維持。同じ固定表を適用して tty・ptmx・shm を閉じるかを別途判断する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-09: 起票。
