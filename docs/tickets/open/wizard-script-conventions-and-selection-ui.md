---
id: wizard-script-conventions-and-selection-ui
title: ウィザード台本の表記規約と選択 UI (矢印選択・色) の整備
status: 設計待ち
priority: 低
opened: 2026-10-16
closed: null
related: [wizard-stage4-menu-per-role, trade-backend-opencode-single-local-path, cli-surface-principle-and-reorg, first-run-setup]
---
# [wizard-script-conventions-and-selection-ui] ウィザード台本の表記規約と選択 UI (矢印選択・色) の整備

**状態**: 設計待ち / **優先**: 低

## 現象

afx init の対話の見た目が揃っていない (2026-10-16 ユーザー要望: 上下キー選択等)。台本の棚卸し (2026-10-16、問いの定義点 53 = text 17 / yesno 15 / choice 13 / typed_yes 1 / 直接 ask 5 / 段 11 指標 2、新規で全 Enter なら実際に出る問いは 21) で見えた不統一: 既定の表記が 4 種 ([Y/n] / [既定: x] / [x] / [Enter]、段 11 は二重表示と空の [])、番号と文字キーの混在 3 か所 (段 3 失敗時 [r]/[2]/[u]、別ホスト失敗時 [r]/[m]/[4]、段 4 の local と 1〜4)、「再試行 / 後で」のキーが d と 4 に割れ r の意味が 5 通り、モデル選択 2 関数で m の可否と文面が違う、q が使えると書いてあるのは typed_yes だけで方針文・最初の課題・段 11 では q が効かない、? が効く問いは 9 つだけ (_HELP_STAGE[8] は未使用)、段見出しは段 0 に無く再実行の段 1 だけ [段 1] 形式、80 桁超の say が 216 本中 63 本、折り返しは端末任せ、色なし。--reset はウィザード本体でなく scripts/first-run-reset.sh (bash)。

## 原因

入力の経路が 6 種 (text / yesno / choice / 番号 2 関数 / 直接 ask) に分かれ、文面と既定の表記を揃える共通層が無い。変更コストの源は受入 doc test (tests/acceptance/test_first_run_acceptance_doc.py) が wizard.py の plain な say・見出し・選択肢行・問いの文面を手順書 docs/operations/first-run-acceptance.md の原文引用と照合すること (文面 1 語で手順書同期が要る) と、prompt 部分一致 168 か所の test。少しずつ直すのが最も高く、台本の完成形を 1 回で決めて一括置換するのが最も安い。

## 処置案・裁定

後回し (2026-10-16 ユーザー裁定: 台本改訂は今回で終わらず、CLI 全体の UX も併せて検討したい)。着手条件: 段 4 再設計 (wizard-stage4-menu-per-role、opencode 1 本化込み) か CLI 整理の段 13 文面で台本に触る機会が来たとき、または CLI UX 全体の規約を決めるとき。進め方は 2 段: 段 1 = 表記規約 (既定の表記 1 形式、全問共通キー q / ? / Enter、段固有キーの意味固定 r 再試行・d 後で・m 手入力、選択は 1 部品で「番号. 名前  一言」行頭 2 桁を印の列として予約し既定行に ▸、問いは「番号 [既定: n]: 」、段見出し「段 N/14 タイトル」、say は 1 描画口で 80 桁折り返し) → 53 問の新文面表を spec 改訂 → 一括実装 + 手順書 + test を 1 束。段 2 = 描画層のみ (WizardIO に select を追加、ConsoleWizardIO を stdlib termios/tty の raw mode で ↑↓ / j k / 数字 / Enter、非 tty・TERM=dumb・raw 失敗時は番号入力へ退避、色は tty かつ NO_COLOR 未設定のみ ANSI、文面には埋め込まない、試験は stdlib pty)。TUI ライブラリ依存は足さない (初回設定の経路に依存を増やさない、WizardIO は秘密を持たない 3 口のまま)。棚卸しの全文は tmp/design-wizard-ui/script-inventory.md (gitignore、375 行)。

## 修正内容

## 経緯

- 2026-10-16: 起票。
