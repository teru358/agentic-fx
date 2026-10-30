---
id: ops-web-head-request-loop-error
title: 同梱 web が HEAD / 早期切断で web_loop_error AttributeError を 1 行出す
status: 是正済
priority: 中
opened: 2026-10-24
closed: 2026-10-30
related: [web-lan-access-bind-allow-auth]
kind: 不具合
---
# [ops-web-head-request-loop-error] 同梱 web が HEAD / 早期切断で web_loop_error AttributeError を 1 行出す

**状態**: 是正済 / **優先**: 中 / **種別**: 不具合

## 現象

稼働中の afx-ops web (main 7f63a4d 時点の venv) に 127.0.0.1:8788 へ GET と HEAD を 1 回ずつ送ると、journal に `afx-ops: web_loop_error AttributeError` が 1 行出る (2026-10-24 09:00 頃、2 回観測)。応答自体は 200。例外型名しか出ないので原因箇所が読めない。web-lan T1 の Host 検査実装で HEAD の扱いを固定する test (web/test_host_policy.py) が入るので、T1 merge 後に再現するかを確認し、残れば request.py の HEAD 経路を直す

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-30: 2 KB 超の応答後の二重 connection_lost を止めた (connection_lost が on_release の前に slot の transport を外す、shutdown の abort は維持)。_loop_error に AttributeError の name と asyncio 定型句の固定語を添え、daemon 呼出しの失敗 (ApiError reason / 非 200 status・code / 形違い) を web_upstream 行として route つきの固定語で stderr に残す (取り消した呼出しは出さない)。2 KB〜20 KB 応答と 50 回繰り返しで loop error なし・slot 漏れなしを test で pin。codex r1 Important 2 → /code-review high 5 (1 件却下) → codex 閉鎖確認で未閉鎖なし。afx-ops 3541 緑

## 経緯

- 2026-10-24: 起票。
- 2026-10-25: 種別: 不具合 — 種別を付与
- 2026-10-30: 優先: 中 — 2026-10-30 再現・原因特定: HEAD は無関係。約 2 KB 以上の応答のあと aiohttp が transport.close() し、buffer が捌けて 1 回目の connection_lost が走り、その finally の on_release → _Service.released → slot.close() → transport.abort() が 2 回目の _call_connection_lost を予約して _protocol / _sock が None で AttributeError (asyncio selector_events.py:910/912、runtime.py:133-134 / 213-217 / 372-377)。応答は送信済みで実害なし、slot / fd 漏れなし。10/24 以降、ブラウザの 30 秒 auto-refresh (render.py REFRESH_SECONDS=30) でページが大きくなり毎回出る。是正案: _Slot.close で transport.is_closing() なら abort しない (+ connection_lost で transport=None)、_loop_error に AttributeError の name を出す、2000 byte 以上の応答で stderr に web_loop_error が出ない test。再現スクリプトは scratchpad webdbg/。
- 2026-10-30: 是正内容 — 2 KB 超の応答後の二重 connection_lost を止めた (connection_lost が on_release の前に slot の transport を外す、shutdown の abort は維持)。_loop_error に AttributeError の name と asyncio 定型句の固定語を添え、daemon 呼出しの失敗 (ApiError reason / 非 200 status・code / 形違い) を web_upstream 行として route つきの固定語で stderr に残す (取り消した呼出しは出さない)。2 KB〜20 KB 応答と 50 回繰り返しで loop error なし・slot 漏れなしを test で pin。codex r1 Important 2 → /code-review high 5 (1 件却下) → codex 閉鎖確認で未閉鎖なし。afx-ops 3541 緑
