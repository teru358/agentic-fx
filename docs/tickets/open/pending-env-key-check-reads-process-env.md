---
id: pending-env-key-check-reads-process-env
title: 未完手順の .env キー判定が process 環境を見るためウィザードで常に「無い」
status: 設計待ち
priority: 中
opened: 2026-10-14
closed: null
related: [first-run-setup]
---
# [pending-env-key-check-reads-process-env] 未完手順の .env キー判定が process 環境を見るためウィザードで常に「無い」

**状態**: 設計待ち / **優先**: 中

## 現象

setup/pending_facts.collect_pending_facts は required_env_keys の有無を os.environ で判定する。ウィザード (afx init) は .env を読み込まないので、段 14 の未完手順に「.env に MT5_BRIDGE_API_KEY がありません」が、.env に鍵があっても必ず出る (段 8 は .env を見て「(あり)」と表示しており矛盾)。daemon は起動時に load_dotenv するので status 側は正しい。是正: 判定を「.env ファイルのキー行 or 環境変数」に (checks.check_env_keys と同じ読み方を共有)、ウィザード・status・splash の 3 経路で同じ結果になる test を足す。2026-10-14 stream-svr 受入 1 回目で発見

## 原因

## 処置案・裁定

## 修正内容

## 経緯

- 2026-10-14: 起票。
