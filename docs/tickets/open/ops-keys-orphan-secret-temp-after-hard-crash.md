---
id: ops-keys-orphan-secret-temp-after-hard-crash
title: 鍵 rotate が key:written 直後に強制停止されると平文の一時 file が鍵 dir に残る
status: 設計待ち
priority: 低
opened: 2026-10-09
closed: null
related: [ops-api]
---
# [ops-keys-orphan-secret-temp-after-hard-crash] 鍵 rotate が key:written 直後に強制停止されると平文の一時 file が鍵 dir に残る

**状態**: 設計待ち / **優先**: 低

## 現象

ops/keys.py の rotate は新鍵を鍵 dir 内の一時 file (.<principal>.token.<uuid>.tmp) に書いてから atomic rename する。例外で unwind する経路は except で一時 file を消すが、SIGKILL や電断では消えず、再実行の rotate は別 uuid の一時 file を作るので孤児が永続する。鍵 dir は 0700 で鍵 file 自体と同じ保護なので露出は増えないが、dir 内に未使用の秘密が残る契約が無い (AC-46 は認証結果のみ)。既存の crash テストは hook 例外で unwind するため観測できていない。対応案: rotate 開始時に自分の principal の *.tmp を unlink してから書く、または .ready 不一致時の案内に孤児 temp の削除を含める。確認は subprocess を key:written で os._exit させて dir を列挙する。

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-09: 起票。
