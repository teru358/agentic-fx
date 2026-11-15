# 影響地図

領域頁は現コードの責務と影響関係を読むための手保守資料です。冒頭で入口、主要 resource、逸脱と未確認を把握し、F（観測事実）、K（承認済み契約）、D（既知の差）と手動辺の根拠を source と照合します。anchor は repository 相対 path と qualified symbol、行番号は参考です。未実装の契約と観測事実を区別します。literal 索引は候補であり、呼出し関係や無関係を証明しません。

## 公開規則

spec・チケットを含む他資料から、本番の時刻・event 行や件数・金額・閾値等の運用観測値を頁へ写さない。契約上の固定 code・仕様定数は区別し、fixture は合成する。鍵・ID・URL・口座・個人設定値・例外自由文を保存しない。必須 link を tmp/、私的 checkout、memory に向けない。公開前に指揮者が確認する。

## extract / evaluate

既存の Python 3 を選び、公開入力だけを指定する。出力は非公開の作業領域に置く。正解表は独立作成・裁定後に指定する。

```sh
python3 scripts/impact_map.py extract --domain kill-switch --page docs/impact-map/domains/kill-switch.md --root src/agentic_fx --root afx-ops/afx_ops --out tmp/impact-map/current
python3 scripts/impact_map.py evaluate --domain kill-switch --index tmp/impact-map/current/index.json --oracle docs/impact-map/oracle/kill-switch-frozen.json --page docs/impact-map/domains/kill-switch.md --out tmp/impact-map/current
```

採点には裁定済みの凍結表 (`kill-switch-frozen.json`、各行に class 欄がある) を使う。A・B の表 (`kill-switch-a.json` / `kill-switch-b.json`) は独立作成の記録で、class 欄が無いので evaluate には渡さない。出力と被覆宣言は人が source と照合する材料で、頁の意味の自動証明ではない。

## 直呼び検査

`tests/scripts/test_impact_map_deviations.py` は `Commands` の StateStore / OutageStateMachine に対する対象メソッドの直呼びを例外台帳と照合する。各関数 scope の単純な名前への代入と注釈付き引数・代入を流れに依らず集め、追跡対象だけの束縛は確定、他の束縛と混在する場合は可能として扱う。未解決と可能は人が確認する。内側関数の引数とローカル名は外側の同名束縛を遮蔽する。

構文検査の保証外は、`getattr` 等による動的取得・呼出し、receiver やメソッドを引数として渡した先での呼出し、属性・コンテナへの代入を経由する alias、`with ... as`、unpack 代入、ループ変数・match capture・代入式など単純な名前への通常代入以外の束縛、関数戻り値や型推論を介した receiver の追跡である。`global` / `nonlocal`、内包表記や lambda 等の独立 scope も正確な解決を保証しない。これらを使う場合は source を人が確認する。

## 手動閉鎖の雛形

1. 予定 symbol と入口・副作用を頁と source で照合し、守る K と禁止経路を brief に渡す。
2. 公開規則、抽出候補、F/K/D と手動辺を全件確認し、不明点を残す。契約・責務・影響関係が変わる場合は code と頁を同じ commit で更新する。
3. 地図を除いた独立初読を保存してから頁差分を照合する。必要な root および afx-ops test を実行し、結果と未検証を記録する。
4. 初回は独立 A/B の裁定、二セルと三分類、履歴再現、実行時間を記録する。未実装の check / affected を実行済みとしない。

`python scripts/impact_map.py verify --page <domain> --anchor <path::symbol>` で照合状態を表示する。
記録するには表示された hash を `--record --expected-hash <hash> --reason <公開可能な理由> --evidence <testまたはticket ID>` と共に指定する。
照合記録は anchor 順の canonical JSON（キー順固定、インデント1、末尾改行）で保存し、一時ファイルから原子的に置換する。成功時の出力は更新後の `fresh` と hash を示す。
記録更新は verification directory のロックで直列化し、同時実行による記録の上書きを防ぐ。
副 anchor は `path::symbol#callee[@literal]` と指定し、該当する最小の statement を照合する。literal は文字列定数の完全一致。
300 行超の宣言に副 anchor がない場合、check は `coarse_symbol` を人が読む項目として表示する。副 anchor が複数に該当する場合は `ambiguous_sub_anchor` を表示する。照合記録のある anchor が曖昧または消失した場合は `stale` も表示する。これらの情報項目だけなら check の終了コードは 0 で、他のエラーがある場合はその終了コードを優先する。

`python scripts/impact_map.py check --page <domain>` で頁、辺、照合記録を検査する。結果は件数と項目の JSON として表示する。

| 終了コード | 意味 |
| --- | --- |
| 0 | 問題なし |
| 1 | stale、根拠消失、必須項目違反 |
| 2 | 解析・宣言・方式・履歴のエラー（1 と同時の場合も 2） |

`python scripts/impact_map.py affected --page <domain> --base <rev> --head <rev>` で revision 間の候補を表示する。`--head` の代わりに `--worktree` を指定すると作業 tree と比較する。
出力は symbol・literal 候補・依存の変化と unmapped を区別する。runtime の候補走査は Python ファイルを対象に、tests、scripts、docs と extract の除外ディレクトリを除く。履歴や解析が取得できない場合は終了コード 2 を返す。

閉鎖時には `symbol_changed`・`coarse_parent_changed`・`ambiguous_sub_anchor`・`base_declarations_partial` を指揮者が必ず source で確認し、必要な履歴と宣言を裁定する。check や affected の終了コード 0 は、これらの照合が済んだことを意味しない。

宣言済み symbol の中に追加された新しい resource は、機械では新 resource と判別できない。`symbol_changed` や副 anchor の親の `coarse_parent_changed` を手掛かりに、その symbol を読んで resource の増減を裁く。副 anchor は最小の statement（多くは単文）の hash であり、外側の制御条件の変化は親の `coarse_parent_changed` に現れる。

記録理由は次の固定値から選ぶ。根拠は存在する `tests/...` のファイル（`::test_name` を付けてもよい）または `docs/tickets/open/`・`closed/` に存在する ticket の ID を指定する。

| 理由 | 用途 |
| --- | --- |
| `initial_record` | 初回照合 |
| `contract_unchanged` | 契約が変わらないことを確認 |
| `relation_unchanged_refactor` | リファクタ後も関係が変わらないことを確認 |
| `relation_changed_page_updated` | 関係の変更を頁に反映して確認 |

AST hash 方式 `v1` は docstring、型注釈、文字列 literal、decorator、default 値を含む AST を保持し、位置情報だけを除く。
規則を変更しない限り方式 version は維持する。
`affected` の `coarse_parent_changed` は副 anchor を持つ親の全体に変更があったことを行数によらず示す情報項目で、辺の変更を意味せず、終了コードを変えない。

`affected` は base に頁がない場合や covers marker が両方ない旧頁の場合、利用可能な宣言で続行し `base_declarations_partial` に理由を出す。この情報項目は終了コードを変えず、壊れた頁は解析失敗として扱う。
