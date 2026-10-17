---
id: first-start-check-times-out-on-model-download
title: 初回起動の確認が埋め込みモデル取得中に timeout して失敗表示になる
status: 設計待ち
priority: 中
opened: 2026-10-17
closed: null
related: [first-run-setup]
---
# [first-start-check-times-out-on-model-download] 初回起動の確認が埋め込みモデル取得中に timeout して失敗表示になる

**状態**: 設計待ち / **優先**: 中

## 現象

受入 3 回目 (2026-10-17、Dunia / Ubuntu 26.04、clean clone)。ウィザード段 13 の起動後確認が「操作 API: failed (not_ready)」「admission: unknown (admission_line_timeout)」と表示。実状態は正常で、起動 66 秒後に plugin sandbox admission ok、85 秒後に improve isolation admitted=yes、その後 afx ctl status も応答。原因は初回起動で chroma の埋め込みモデル all-MiniLM-L6-v2 (79MB) を ~1MB/s で取得していたため。setup/post_start.py の待ちは API 30 秒 / admission も同程度で、初回限定の取得時間 (回線次第で 1〜3 分) を吸収できない。利用者には『失敗』と読める (段 14 の未完の手順にも『操作 API の起動確認に失敗しました』が残る)。

## 原因

埋め込みモデルの取得が daemon の初回起動時に遅延実行される設計で、ウィザードはそれを知らずに固定 30 秒で判定している。2 回目以降の起動 (cache あり) では再現しない。

## 処置案・裁定

設計事項 (小): (a) モデル取得をウィザード段 10〜11 (uv sync 済み・ネットワーク前提の区間) で先に行い進捗を見せる、または daemon 起動前に `afx check` 相当で取得する。(b) 取得が未了なら post_start の待ちを延ばし (cache ディレクトリの有無で判定、上限 300 秒) 『初回は埋め込みモデルの取得で数分かかります』と表示。(c) 判定が timeout のときの文言を『失敗』でなく『確認できませんでした (起動は続いています)』に。受入は main-claude 台本で再現可 (clean clone + ~/.cache/chroma を消す)。

## 修正内容

## 経緯

- 2026-10-17: 起票。
