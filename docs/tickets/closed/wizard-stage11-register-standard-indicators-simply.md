---
id: wizard-stage11-register-standard-indicators-simply
title: 段 11 は「標準指標 9 本を登録します [Y/n]」の 1 問に
status: 是正済
priority: 低
opened: 2026-10-14
closed: 2026-10-16
related: [first-run-setup, wizard-stage9-summary-in-user-terms]
---
# [wizard-stage11-register-standard-indicators-simply] 段 11 は「標準指標 9 本を登録します [Y/n]」の 1 問に

**状態**: 是正済 / **優先**: 低

## 現象

段 11 は一覧 → 外す番号の入力 → 「9 本を採用します…(decided_by=human_cli)。よろしいですか [y/N]」の 2 問で、基本指標を外す理由が普通は無いのに手数が多く、内部語も出る。一覧を見せて「標準指標 9 本を登録します [Y/n] (e = 外す番号を選ぶ)」の 1 問にし、既定 Y。採用が人の確認として記録される点は 1 問で担保。審査中の進捗表示は維持 (1 行ずつでよい)。再実行で配備済みのものは「配備済み」と添えて再採用は尋ねない。2026-10-14 stream-svr 受入 1 回目のユーザー指摘

## 原因

## 処置案・裁定

## 修正内容

- 2026-10-16: 段 11 は「標準指標 N 本を登録します [Y/n] (e = 外す番号を選ぶ)」の 1 問 (告知行つき)、全部配備済みなら 1 行、EOF は中断契約に乗る。main 42a1ded (fr-fix2、spec v1.8。codex r1 → /code-review high r2 → codex r3、是正 3 周)

## 経緯

- 2026-10-14: 起票。
- 2026-10-15: 優先: 低 — 再実行では 9 本すべてに「配備済み・既定で外れています」と 2 行の警告が 9 回繰り返され、最後に「採用する指標がありません」。再実行時は「標準指標 9 本は配備済みです (変更なし)」の 1 行で済ませ、切り替えたい人だけ e で一覧へ (2026-10-15 受入 2 回目)
- 2026-10-15: 状態: 実装中 — 束 fr-fix2 (spec v1.8、ブリーフ tmp/review-20261011-sc/fr-fix2-1.md / fr-fix2-2.md)
- 2026-10-16: 是正内容 — 段 11 は「標準指標 N 本を登録します [Y/n] (e = 外す番号を選ぶ)」の 1 問 (告知行つき)、全部配備済みなら 1 行、EOF は中断契約に乗る。main 42a1ded (fr-fix2、spec v1.8。codex r1 → /code-review high r2 → codex r3、是正 3 周)
