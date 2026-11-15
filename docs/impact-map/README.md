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

## 手動閉鎖の雛形

1. 予定 symbol と入口・副作用を頁と source で照合し、守る K と禁止経路を brief に渡す。
2. 公開規則、抽出候補、F/K/D と手動辺を全件確認し、不明点を残す。契約・責務・影響関係が変わる場合は code と頁を同じ commit で更新する。
3. 地図を除いた独立初読を保存してから頁差分を照合する。必要な root および afx-ops test を実行し、結果と未検証を記録する。
4. 初回は独立 A/B の裁定、二セルと三分類、履歴再現、実行時間を記録する。未実装の check / affected を実行済みとしない。

`python scripts/impact_map.py verify --page <domain> --anchor <path::symbol>` で照合状態を表示する。
記録するには表示された hash を `--record --expected-hash <hash> --reason <公開可能な理由> --evidence <testまたはticket ID>` と共に指定する。
300 行超の symbol は `coarse_symbol` として表示する。
