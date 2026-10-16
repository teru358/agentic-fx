---
id: wizard-stage8-ask-daemon-not-api
title: 段 8 は操作 API でなく「常駐サービスで運用するか」を問う
status: 是正済
priority: 中
opened: 2026-10-14
closed: 2026-10-16
related: [first-run-setup, wizard-stage4-menu-per-role]
---
# [wizard-stage8-ask-daemon-not-api] 段 8 は操作 API でなく「常駐サービスで運用するか」を問う

**状態**: 是正済 / **優先**: 中

## 現象

段 8 の説明 (UNIX ソケット・TCP では開かない・afx ctl・bot/web/notify が使えない…) は利用者の判断軸と合わず分かりにくい。利用者が決めたいのは「常駐 (daemon) で運用するか、手で起動する対話シェルで使うか」で、操作 API の有無はその結果。問いを「afx を常駐サービスとして動かしますか [Y/n]」に置き換え、Y → api.enabled true + 段 13 の unit 設置を前提 (段 13 は意思を再質問せず設置内容の確認だけ)、n → 対話シェル運用として api.enabled false・unit は置かず手動起動の案内。.env のキー確認はそのまま。spec §3 段 8 / 段 13 と §5 の書き込み先表の改訂を伴う。2026-10-14 stream-svr 受入 1 回目のユーザー指摘

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-16: 段 8 は「afx を常駐サービスとして動かしますか [Y/n]」。Y → api.enabled true + 段 13 で unit (再質問なし)、n → api false + 手動起動の案内 (既存 unit は残す旨と止め方)。main 42a1ded (fr-fix2、spec v1.8。codex r1 → /code-review high r2 → codex r3、是正 3 周)

## 経緯

- 2026-10-14: 起票。
- 2026-10-15: 状態: 実装中 — 束 fr-fix2 (spec v1.8、ブリーフ tmp/review-20261011-sc/fr-fix2-1.md / fr-fix2-2.md)
- 2026-10-16: 是正内容 — 段 8 は「afx を常駐サービスとして動かしますか [Y/n]」。Y → api.enabled true + 段 13 で unit (再質問なし)、n → api false + 手動起動の案内 (既存 unit は残す旨と止め方)。main 42a1ded (fr-fix2、spec v1.8。codex r1 → /code-review high r2 → codex r3、是正 3 周)
