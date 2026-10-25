# [first-run-setup] 初期設定ウィザード 設計書 (v1.8)

作成日: 2026-10-02 (v0.1)。v0.2 改訂: 2026-10-12。v0.3 改訂: 2026-10-12。v0.4 改訂: 2026-10-12。v0.5 改訂: 2026-10-12。対象 commit: main `f01f903` (+ 未追跡 `deploy/`)。v0.1 は `3b4213d` を対象にした。
入力: `tmp/design-first-run/inventory.md` (現状の全数調査)、`tmp/design-first-run/wizard-flow-and-decisions.md` (叩き台・論点・**末尾の裁定記録**)、B-2 実測 `tmp/design-first-run/b2-measure/` (生ログ `run1.log` `run2.log`、本書 付録 A に要約)。
v1.0 は 2026-10-12 にユーザー承認済み (設計レビュー 3 周: Critical / High なし)。草稿は `tmp/design-first-run/C0.md` (v0.1〜v0.5)。現物と照合した記述は `path:行` を添える。照合できなかったものは「未確認」と書く。
v0.2 で引き直したのは、常駐化 (direct exec の user unit)、外から見る手段 (`afx ops enable`)、改善 backend の疎通確認と隔離適合の見方、秘密の置き場、承認の導線に関わる記述。それ以外の `path:行` は v0.1 の時点の番号で、`service.py` を中心に数行〜数十行ずれているものがある (例: `ensure_initialized` は 87 行、`_check_mission_ceilings` は 693 行、`build_app` は 832 行)。実装では行番号でなく関数名で引き直す。
v0.3 で引き直したのは、確定 1 の原子性の表現 (回復可能な commit protocol)、排他 (`setup.lock → instance.lock` の無隙間の引き渡し)、履歴源 (`backtest.eval_source: none`)、起動後確認 (3 段階)、settings の更新追随 (`schema_version` と migration)、`afx ops enable` との境界 (wizard の IO と ops の IO の分離、`EnableResult`、lease)、操作 API の status 項目、journal の admission 行の帰属、実機受入の方針と手順書、設定のリセット (v0.3 では内蔵の `afx init --reset`)。

v0.4 で引き直したのは、サービスの起動ガード (marker の検査を設定の読み込みより前の 1 箇所に置く、§4.3)、リセットの手段と所有範囲 (独立スクリプト `scripts/first-run-reset.sh`、当 instance の資材だけを消す、§4.5)、linger の扱い (ウィザードが確認つきで有効化し、所有記録がある場合だけリセットが解除する)、local LLM が無い環境を主シナリオにした段 4 と受入手順書、journal の観測契約 (§6.4)、settings schema を変える変更の運用責任 (§4.4)、実装 task の分割 (§9)、実機受入の「試験環境の都合」と自動化の範囲 (§8.4)。

v0.5 で引き直したのは、LLM 未検証の扱い (`afx check` の結果を fatal と warning に分け、warning だけなら unit を設置して degraded で起動する。Claude を選んだときはウィザードが消費確認のうえ trade 用の `claude -p` を 1 往復する、§3 段 4・§6.2)、リセットの所有判定の実装契約 (bash は orchestration だけ、所有の判定は `python3 -I` の標準ライブラリ、ops の config は canonical な 1 行の一致だけを owned とする、§4.5)、自動化 driver のリセットコマンドと linger の反復試験 (§8.4、AC-39)、リセットの製品契約と試験の clean 判定の分離 (AC-36)、実装 task ごとの「所有する受入条件」と共有ファイルの関数単位の責務、実装順序 (§9)、未決事項の仕分けと決定記録 (§10)。本文の規範は B / P / U / T の番号だけで挙動を決めず、意味のある名前を併記する。番号は履歴と参照の索引にすぎない。

## 現行 CLI の追補

ウィザードを起動する argv は `afx init`（repo の開発環境では `uv run afx init`）である。案内する backend の実往復確認は `afx check backend --backend <local|claude|codex|opencode> [--provider chatgpt]`、後からの改善課題追加は `afx ctl backlog add <idea>` とする。段 12 で確定した方針文は `ops_policies` に記録され、ID marker 付きの managed 方針として投影される。外から見る手段を有効にする案内は `afx ops <feature> enable` の機能→動詞形とする。

## 要点

1. 初めての利用者が `afx init` を 1 回打てば、対話で「価格をどこから取るか・どの LLM で判断と改善をするか・何分足で判断するか・いつ改善を回すか」を決め、各項目をその場で接続試験し、結果を読める文面で見せる。従来の非対話 init は `--non-interactive` として残す (B-1 a、B-3 b)。
2. 設定ファイルは example を写し、**答えた項目の行だけ**を書き換える。触らない行はバイト一致 (B-2、付録 A の実測: 14 項目で 12 行だけ変わる)。書く前に重複キー検査・意味的照合・型検証の 3 つを必ず通す。
3. 書き込みは「集めてから確定する層」(設定・DB・状態) と、その後の「重い・人の確認が要る段」(標準指標の採用・方針文・最初の課題・常駐化) の 2 層に分ける。前者は複数のストレージ (YAML・SQLite・JSON) にまたがるので 1 つの transaction にはできない。そこで**回復可能な commit protocol** にする: durable な marker を最初に置き、marker がある間はサービスが専用の終了コードで起動を拒否し、最後に marker を消す。どこで落ちても `afx init` の再実行で収束する (§4.3)。後者は 1 件ずつ独立に確定する。
4. 外部接続 (LLM・MT5 bridge) が繋がらなくても完了できる (B-6 b)。繋がらなかった項目は確認記録に残り、`status` と起動画面が「未完の手順」として出し続ける。
5. MT5 bridge は 3 分岐 (接続済み / 使う予定だが未準備 / 使わない) で扱う。未準備なら価格源は MT5 のまま「未接続」で完了し、afx は現物どおり停止状態 (degraded) で起動して、bridge が繋がったら自動で足を取り込み再開する。bridge 無しでは戦略の採用審査に要る履歴が取れないことを、できないこととしてはっきり表示する。Dukascopy は勧めず、使わない分岐では `backtest.eval_source: none` を書いて設定でも「履歴源なし」を表す。
6. 初回起動の直後に改善 mission が勝手に 1 本走る現象 (catch-up) は、ウィザードが「今期は消費済み」の印を DB に置くことで止める (B-8)。利用者が「今すぐ 1 本」を選んだときだけ印を置かない。
7. 常駐化は unit の内容を見せて確認してから置く (B-4 b)。unit は direct exec (`Type=exec`、`.venv/bin/python main.py --daemon`) で、対話シェルは持たない (B-5 は daemon に確定)。承認・方針文・改善の起動は `afx ctl` で行う。unit を置いたら、外から見る / 操作する手段 (bot / web / notify) を一問だけ尋ね (既定 = あとで)、選ばれた機能は既存の `afx ops enable <feature>` に任せる。質問と疎通確認はそちらが持ち、ウィザードは重複して尋ねない。
8. ウィザード本体は秘密を受け付けず、書かず、表示しない (B-13)。Discord token・通知 webhook URL は `afx ops enable` が feature 専用の secret (`~/.config/agentic-fx/ops/<feature>/secret/`、0600) に書く。MT5 bridge / Twelve Data の API キーなど `.env` に置くものは、足りないキー名を知らせ、存在だけ確認する。
9. 排他は `data/ops/setup.lock → data/instance.lock` の順に統一する。`afx init` は setup.lock を最後まで保持し、`afx ops enable` には「保持済みの setup lease」を渡して隙間なく引き渡す。service の起動は、引き渡しの間、marker (終了コード 76) で塞ぐ (§4.2)。
10. 起動後の確認は 3 段階 (サービスが active / UDS + 鍵認証の `afx ctl status` が有界時間内に成功 / 選んだ ops feature が active かつ ready) で、それぞれ別に記録する。
11. 実機受入は別マシンで、Claude の記憶に頼らず repo の案内だけで clean clone から立ち上げる。受入手順書 `docs/operations/first-run-acceptance.md` をこの束の成果物にし、足りない知識が出たら案内の欠陥として直す (§8.4)。やり直しのために、当 instance の設定・データ・設置した unit・鍵を clean clone の状態に戻す独立スクリプト `scripts/first-run-reset.sh` を同じ束で作る。venv も settings も前提にせず、他の instance の資材には触れない (§4.5)。
12. 主シナリオは local LLM が無い環境 (取引判断は `claude -p`、改善は Claude または Codex)。段 4 は llama-swap が応答しないとき local を既定にせず、「Claude を選ぶ / 後で設定する」を明示的に選ばせる。CLI が未導入・未ログインでも安全に完了し、`bin_not_found` / `credentials_missing` と repo 内の手順への導線を残す。LLM が未検証なだけなら unit は設置し、起動後は degraded と未完の手順を表示する (fatal と warning の区別、§6.2)。Claude を選んだときだけ、ウィザードが消費確認を出して `claude -p` を 1 往復する (§3 段 4、§8.4)。

---

## 1. 目的と範囲

### 目的

clone した人が README を読まなくても、`afx init` の対話だけで「ペーパー運用が回り始め、何が未完かが分かる」状態に到達させる。製品像は「使う人が育てるシステム」(memory `product-vision-grown-by-its-user`): すぐ取引・すぐ勝てるは目指さない。既定値は安全側、失敗したら理由が読めること。

### 範囲

- `afx init` (= `uv run afx init`) の対話化。段ごとの接続試験と読める文面。
- `config/settings.yaml` の生成と、再実行時の部分書き換え。
- DB 初期化・基本ニュースソース・状態ファイル (従来の init がやっていたこと)。
- 確認記録 (どの項目を、いつ、どう確認したか) の保存と、`status`・起動画面での「未完の手順」表示。
- 改善周期と「最初の 1 本をいつ走らせるか」。
- 標準指標 9 本の採用 (一覧を見せ、明示確認を取ってから既存の bless 経路で 1 本ずつ)。
- 方針文 (`policy/directives.md`) と最初の課題 (backlog) の受け付け。
- systemd user unit (direct exec の daemon) の生成・設置・有効化 (確認つき)。
- 外から見る / 操作する手段 (bot / web / notify) の入口: 一問尋ね、選ばれた機能について既存の `afx ops enable <feature>` を呼ぶ。
- MT5 bridge を「配置して afx とつなげるまで」の案内文。
- 既存の非対話 init の挙動を `--non-interactive` として保つこと。
- settings の `schema_version` と、版ごとの明示 migration による再実行時の追随 (§4.4)。
- 設定のリセット用の独立スクリプト `scripts/first-run-reset.sh` (実機受入のやり直し用。bash と最小の python3 呼び出しだけで動き、venv も `Settings` も前提にしない。§4.5)。
- 実機受入の手順書 `docs/operations/first-run-acceptance.md` (§8.4)。

### 範囲外

- Wine と MT5 本体の導入 (利用者ごとに環境が違う。裁定 2026-10-02)。
- bot / web / notify そのものの質問・疎通確認・有効化の状態機械 (`afx ops enable` が持つ。afx-ops spec `docs/superpowers/specs/2026-10-09-afx-ops-design.md` §6.1・§7)。ウィザードは呼ぶだけで、設定項目や疎通確認を再実装しない。
- ウィザード本体での秘密の入力・保存 (B-13)。MT5 bridge / Twelve Data の API キーは利用者が `.env` に書く。Discord token・通知 webhook URL の入力は `afx ops enable` が行う (非エコー入力、secret file は 0600)。
- 履歴の取り込みそのもの (`afx history import`) の実行。ウィザードは手順を示すだけ (理由は §3 段 6)。
- 設定とデータの整合を検出する一般的な仕組み (inventory C-6)。本設計は「注意が要る項目を再実行で変えるときの確認文」だけを持つ。
- 戦略 plugin の同梱 (方針: 生成コードはリポジトリに入れない)。
- `mode trading` への遷移 (未実装、ペーパー専用のまま)。
- ニュースソースの追加・品質評価・自動採用 (ウィザードは基本ニュースソースの seed だけを行う。ニュースソース追加は人の運用操作で、改善ループの経路ではない)。
- strategy の生成物の自動採用 (改善ループの出力は既存の plugin gate と人間承認を迂回しない。ウィザードが採用するのは標準指標だけで、既存の bless 経路を明示確認つきで通す)。
- Pine Script の入力・変換・配備。
- 内蔵の `afx init --reset` (低チケット `afx-init-reset-builtin`。リセットは独立スクリプトを主にする。§4.5、§10.3)。

---

## 2. 前提を疑った結果: 現状のどこで初めての利用者が詰まるか

| # | 詰まる所 (現物) | 何が起きるか | 本設計での扱い |
|---|---|---|---|
| P1 | README が 0 バイト、runbook 無し (inventory §1.3) | `init` の存在を知る手段が無い | README に入口 3 行を置く (task T8)。入口は `afx init` 1 本 |
| P2 | `init` は example を丸ごとコピーし、**編集前の値のまま検証・DB 作成・`initialized=true` まで進む** (`service.py:412-480`) | example のモデル名 `qwen3.6-35b-a3b_Q4` (作者の環境の名前)、`display_timezone: Asia/Tokyo`、`eval_source: dukascopy` のまま「初期化が完了しました」と出る | 対話で尋ねて書き換える。答えなかった項目は example の値のまま残るが、外部に依存する項目は「未確認」として記録に残す |
| P3 | 接続失敗は init 出力の 1 回だけの警告 (`service.py:452-462`、inventory C-2) | 見落とすと mission が失敗し続けて初めて気づく | 確認記録 + `status`・起動画面の「未完の手順」に出し続ける |
| P4 | 設定エラーは pydantic の英語 traceback (`service.py:422` は try 無し、inventory §4.1 i2) | 何を直せばよいか読めない | ウィザードは書く前に dict で検証し、キーごとに日本語で言い換える |
| P5 | improve モデルの存在・CLI backend の bin と認証ファイルは init では見ない (起動時 `service.py:1031-1032` で初めて) | init は通るのにサービスが起動拒否 → unit なら 30 秒ごとの再起動ループ (`Restart=on-failure` `RestartSec=30`、inventory C-7) | 段 4 で trade・improve 両方を試す。unit を有効化する前に、unit と同じ環境で起動前検査を通す (`afx check`、§6) |
| P6 | `runner.codex.bin` は example でプレースホルダ (`/path/to/vendor/...`)、`opencode.context_limit: 0` | codex / opencode を選ぶと起動拒否。値をどこから取るか書かれていない | 段 4 で選ばれた backend の項目だけ尋ねる。opencode は llama-swap の `/props` から ctx を提案 (ロードの副作用は確認つき) |
| P7 | 判断足 5m 以下は既定の timeout で起動拒否 (`service.py:680-696`) | 設定時ではなく起動時に分かる | 段 5 で同じ計算 (`core/mission_ceiling`) をして、その場で理由と代案を出す |
| P8 | 初回起動の直後に改善 mission が 1 本走る (`core/improve_supervisor.py:88-110`、ticket `improve-catchup-runs-at-startup`) | 方針文・最初の課題を入れる前に走り、CLI backend ならサブスク枠を消費する | 段 7 で「最初の 1 本をいつ」を尋ね、既定は「次の予定まで待つ」(§3 段 7、B-8) |
| P9 | 履歴の取り込みが案内されていない (inventory §1.3-8) | 改善 mission の backtest が「履歴 0 件」で失敗する。理由が mission の中に埋もれる | 段 6 で履歴源ごとに「できること / できないこと」を表示し、`status` に「履歴なし」を出し続ける |
| P10 | `eval_source` の既定が `dukascopy` (`settings.yaml.example:43`) | 無料公開サービスに 1 秒間隔・最大 500 リクエスト/回を投げる導線になる。この環境は遮断の前歴あり (memory `dukascopy-likely-blacklisted`) | ウィザードは Dukascopy を選択肢の既定にしない。MT5 を使わない分岐では `backtest.eval_source: none` を書いて「履歴源なし」と表示 (§3 段 6) |
| P11 | 本番の unit の写し (`deploy/afx.service.direct`) は作者のパスと nvm のバージョンを固定で持つ (`WorkingDirectory=%h/project/agentic-fx`、`ExecStart=%h/project/agentic-fx/.venv/bin/python main.py --daemon`、`PATH=%h/.nvm/versions/node/v24.18.0/bin:...`、`After=mt5-bridge.service` も無条件) | 他人の環境ではそのままでは動かない。node を更新すると claude/codex が見つからず起動拒否ループ | unit の値はすべて実行環境から検出して埋める (§6)。固定行 (`Type=exec`、`Delegate=pids memory`、`RestartPreventExitStatus=75` など) だけを写す |
| P12 | `afx` は cwd を root にする (`entry.py:23`) | リポジトリ外で打つと、そこに `config/` `data/` `logs/` を作る | 段 0 で cwd に `config/settings.yaml.example` が無ければ何も作らず止まる |
| P13 | 非 tty だと黙って daemon (`entry.py:28`、ticket `legacy-e2e-daemon-quiet`) | init 自体は daemon 判定より前に分岐する (`entry.py:24-25`) ので影響しないが、対話化すると「非 tty のときどうするか」が新たに要る。tty の有無で黙って分岐すると同種の事故 (30 分の誤診) を繰り返す | ウィザードは非 tty かつ `--non-interactive` 無しなら、理由を書いて rc=2 で止まる (黙って分岐しない) |
| P14 | 作者の環境は `initialized=true`・稼働中の unit あり (inventory C-1) | ウィザードを必須にしても発火しない。逆に再実行で既存 settings や unit を壊す恐れ | 再実行は答えた行だけ変える (B-2)、unit は差分を見せて確認、稼働中は排他で拒否 (B-12)。テストは tmp の root だけで行う |
| P15 | `mt5_bridge/README.md` は finance 時代の手順 (`git clone <finance-repo-url>`、`feature/mt5-integration`、設定キー `mt5_bridge:` は旧 finance の形) | 書いてある設定を写すと agentic-fx では未知キーで起動拒否 (`extra="forbid"`) | bridge の案内はウィザード内の文面で完結させる。README の改訂は別チケット候補 (§10) |
| P16 | `config/settings.yaml.example` の `api.enabled` は `false` (`config.py` の既定も `false`)。コメントは「改善 worker の seccomp が入るまで false」だが、改善 CLI 隔離は有効化済み (`improve_admission.ISOLATION_ENABLED`) | example のまま daemon を常駐させると操作 API の listener が開かず (`service.py` の `start_ops_api` が警告だけで返る)、`afx ctl` も bot / web / notify も接続できない。承認ができない常駐になる | 段 8 で操作 API を尋ね (既定 = はい)、`api.enabled: true` を書く。example の既定と注釈は実装 task T8 で `true` に直す (§9) |
| P17 | `afx ops enable` は PATH 上の `afx-ops` か `afx_ops` module を探す (`ops_setup/units.py` の `resolve_ops_exec`)。afx-ops は本体と別の venv (`afx-ops/`) で、どちらも無ければ `ops_exec_not_found` | bot / web / notify を選んでも、enable が入口で止まる | 段 13 で enable を呼ぶ前に同じ解決を試し、見つからなければ置き方を案内して「あとで」に倒す。完了は妨げない |
| P18 | `afx ops enable` は `afx.service` という名前の unit を停止・照会し、drop-in `afx.service.d/afx-ops.conf` で `Wants=` を足す (`ops_setup/stages.py`、`UnitPaths.afx_unit` の既定)。unit 名は変えられない | unit が先に在ることが前提 (無い unit への `systemctl stop` の挙動は未確認)。別名の unit を置く試験も本番の enable では使えない | 段 13 は unit の設置を先に、ops の有効化を後にする。unit 名は `afx.service` 固定 |
| P19 | 既存の `settings.yaml` は、更新後の example が足した必須キーを持たない。行単位の書き換えだけでは新キーを導入できず、G3 で止まる | 将来 schema が増えるたびに再実行が失敗する | `schema_version` と版ごとの migration (§4.4) |
| P20 | 確定 1 は settings・DB・JSON を順に書く。再実行では `initialized=true` が既に立っているので、途中で落ちると起動ガードは新旧混在の状態を止めない | 設定と DB が食い違ったままサービスが起動する | 最初に durable marker を置き、サービスは marker がある間は終了コード 76 で起動を拒否する (§4.3) |
| P21 | 実機受入を繰り返すには、設置した unit・鍵・設定・データを毎回手で消す必要があり、取りこぼしが次の試行を汚す。しかも戻したいのは「ウィザードが途中で壊れた」状態で、venv や settings.yaml が読めないことがある | 試行が clean clone を再現しない。稼働中の環境や、同じ user の別 instance の鍵・設定を誤って消す事故の恐れ | 独立スクリプト `scripts/first-run-reset.sh` (§4.5)。venv も settings も前提にしない。消すのは当 instance の資材だけで、全数を表示して確認を取り、稼働中なら別に停止確認を取る |

---

## 3. 対話の流れ

### 3.0 全体の形

```
[集める層: 何も書かない]
 段0 前提確認 → 段1 はじめに → 段2 基本 → 段3 価格源 → 段4 LLM → 段5 判断足
 → 段6 履歴と backtest → 段7 改善周期と最初の1本 → 段8 操作 API と .env → 段9 要約と最終確認
[確定 1: 回復可能な commit protocol (marker → settings → DB → 記録 → 状態 → marker 除去)]
 段10 設定・DB・確認記録・期の消費印・状態 を順に確定する
[確定 2: 1 件ずつ独立]
 段11 標準指標の採用 → 段12 方針文と最初の課題 → 段13 常駐化と外から見る手段 → 段14 次の一歩
```

段ごとに「なぜ聞くか / 後で何に効くか」を 1〜2 行添える。各段で `?` を打つと詳しい説明、`q` で中断 (集める層なら何も残らない旨を表示)。入力は番号選択と短い文字列だけ (秘密は扱わない)。

**再実行 (B-11) のとき**: 各段の既定値は「現在の settings.yaml の値」。段 1 は 1 行に短縮。段 10 以降は「変わった項目だけ」を書く。詳細は §4。

### 段 0: 前提確認 (質問なし)

- **表示**: 「agentic-fx の初期設定を始めます。途中でやめても、段 10 で確定するまでは何も書きません。」に続けて検査結果を 1 行ずつ。
- **検査と文面**:
  - cwd に `config/settings.yaml.example` が無い → 「ここは agentic-fx のリポジトリ直下ではありません (config/settings.yaml.example が見つかりません)。clone したディレクトリで実行してください。」rc=2。**何も作らない** (P12)。
  - 排他 (B-12、§4.2): 先に `data/ops/setup.lock` を非ブロッキングで取り、次に `data/instance.lock` を取る (この順序は固定)。setup.lock が取れなければ「別の `afx init` または `afx ops enable` が実行中です。終了してから再実行してください。設定・DB は変更していません。」rc=2。instance.lock (`store/instance_lock.py:24`) が取れなければ「別の agentic-fx が動いています。`systemctl --user stop afx` で止めてから再実行してください (`uv run afx` で手動起動しているなら、その端末で終了してください)。設定・DB は変更していません。」rc=2。取れた 2 つの lock は、§4.2 の引き渡しの規則に従ってウィザード終了まで保持する。注: lock を取るために `data/` と `data/ops/` ディレクトリが作られる (`instance_lock.py:33` の mkdir と、afx ops enable の setup.lock と同じ fd 相対・`O_NOFOLLOW` の作法)。これは集める層で唯一の書き込みで、中身は空の lock ファイル 2 つだけ。
  - 非 tty で `--non-interactive` 無し → 「対話が必要です (標準入力が端末ではありません)。自動構築では `afx init --non-interactive` を使ってください。」rc=2。
  - Landlock (`core/landlock.is_available()`) が偽 → 「この環境では Landlock が使えません (x86_64 かつ ABI 3 以上が必要)。標準指標の導入と改善ループは使えません。取引判断のペーパー運用だけ設定できます。続けますか [y/N]」。続けた場合、段 7・段 11 は説明だけ出して飛ばす。
  - Landlock は使えるが改善 worker の隔離が要求する ABI (`core/improve_admission.MIN_LANDLOCK_ABI` = 8、Linux 6.18 以降) に届かない (`core/landlock.landlock_abi()` < 8) → 「この kernel (Landlock ABI N) では改善ループの隔離が使えません (ABI 8 以上、Linux 6.18 以降が必要)。取引判断と標準指標は設定できます。改善は kernel を更新するまで止まります。」と表示して続行 (質問なし)。記録の `landlock` は `ok` のまま reason `landlock_abi_too_old` を添え、未完の手順に `improve_isolation_unavailable` を出す。段 4 の実往復 (`verify-backend`) の問いは出さず、段 7 は周期を尋ねたうえで「この kernel では改善は走りません」を 1 行添える (v1.7)。
  - pytest が import できない → 「plugin の採用審査 (gate) が pytest を使います。`uv sync` (dev 依存を含む既定) で入ります。」(警告、続行可。`uv sync --no-dev` で本当に落ちるかは未確認 — inventory C-9)
  - 未終端の `afx ops enable` (enroll 中の印 `data/ops/enroll_in_progress`、`store/enroll_marker.py`) → 「`afx ops enable <feature>` が途中で止まっています。同じコマンドを再実行して完了させてください。その間 afx は起動できません。」rc=2。印があるとサービスは終了コード 75 で即終了するので、先に進んでも段 13 の起動が成立しない。
  - 未終端の確定 (`data/state/setup_in_progress`、§4.3) → 「前回の初期設定が途中で止まっています。このまま続けて収束させます (サービスはこの印がある間、起動を拒否します)。」と表示して続行する (rc=2 で止めない。回復はこの再実行が担う)。印が壊れていて読めないときも同じ。再開地点の決め方は §4.3。
  - settings.yaml の版 (`schema_version`、§4.4): 現行の版より古ければ migration の予定を段 9 の要約に出すと表示する。新しければ「この afx より新しい版の settings.yaml です。afx を更新してください」で rc=1。未知の手編集の形で migration の位置が決まらないときは、何も書かず、手で直す箇所を具体的に示して rc=1。
  - Python・OS: 表示のみ。
  - 既存 `settings.yaml` の有無: 有れば「再実行です。現在の値を既定として表示します」(§4)。重複キーがあれば (付録 A の G1) 「settings.yaml の <キー> が 2 回書かれています (行 N と M)。現在は後の行の値が効いています。どちらかを消してから再実行してください。」rc=1。
- **書き込み**: lock ファイル 2 つ (`data/instance.lock`、`data/ops/setup.lock`) のみ。

### 段 1: はじめに (読み物)

- **表示の要旨**: これは「使う人が育てる」システムで、すぐ取引・すぐ勝てるものではない。まずペーパー (実資金を動かさない)。発注・損切り・資金保護は LLM ではなく決められたコードが行い、drawdown kill switch は設定で切れない。戦略や指標の採用は必ずあなたの明示確認を経る。改善ループは LLM を使うので、CLI backend (claude / codex) を選ぶとサブスクの枠を使う。
- **尋ねること**: 「読みました」の確認 [Enter]。
- **再実行**: 1 行に短縮。

### 段 2: 基本

| 尋ねること | 既定 | 理由 | 検査と失敗時の文面 |
|---|---|---|---|
| 表示タイムゾーン | OS のタイムゾーン (`/etc/localtime` の IANA 名、取れなければ `UTC`)。再実行は現在値 | example の `Asia/Tokyo` は作者の値。改善の時刻 (`improve_at`) もこの TZ で解釈される (`scheduler.py:1414-1441`) | IANA 名か。「'JST' は使えません。'Asia/Tokyo' のような地域名で入力してください」 |
| 口座通貨 | `JPY` (example) | 損益・リスクの基準。**後から変えると過去の損益と通貨が混ざる** (inventory §8.2) | `^[A-Z]{3}$`。「3 文字の大文字で (JPY, USD …)」 |
| 対象ペア | `[USDJPY]` | 運用実績は USDJPY のみ | `risk.pair_rules` にあるペアだけ選択肢に出す (現 example では USDJPY / EURUSD)。複数選択時は「クォート通貨が口座通貨と違うペアは換算を通ります。運用実績は USDJPY のみです」 |
| 初期残高 (ペーパー) | `1000000` (example) | **後から変えると過去の残高・HWM・kill switch 判定の基準が動く** (`paper_broker.py:46`) | 正の数 |

- **書き込み**: なし (段 10 でまとめて)。

### 段 3: 価格源 (B-7 の 3 分岐)

- **表示の要旨**: 「取引判断と約定判定に使う価格をどこから取るかを決めます。本線は MT5 bridge (MetaTrader 5 から価格を受け取る小さな HTTP サーバ) です。」
- **尋ねること**: 「MT5 bridge を使いますか」
  1. 使う。bridge は動いている
  2. 使う予定。まだ準備していない
  3. 使わない
- **既定**: 再実行なら現在の構成から推定 (mt5 enabled なら 1、それ以外は 3)。初回は既定なし (必ず選ばせる)。理由: どれを選んでも後の振る舞いが大きく違い、黙って決めると「なぜ止まっているか」が読めなくなる。

**分岐 1 (使う・接続済み)**
- 尋ねる: bridge の URL (既定 `http://localhost:8812`)。bridge 側で API キーを設定したか (はい / いいえ / 分からない)。
- 検査: URL に userinfo (`user:pass@`) があれば拒否 (「URL に認証情報を入れないでください。API キーは `.env` の MT5_BRIDGE_API_KEY に書きます」、inventory C-3)。続いて接続試験: `GET {url}/health` → `mt5_connected` を見る、次に `sources.mt5_quote(url, pairs[0])` (`datafeed/sources.py:107`) で 1 本取得。**DB を開かずに adapter を直接呼ぶ** (段 10 より前に `data/agentic.db` を作らないため。現行 init の `PriceProvider.healthcheck` は DB 接続が要る — inventory §3.4)。
- 成功: 「MT5 bridge OK: USDJPY bid=… ask=… (bridge の時刻 …、UTC で …)」。
- 失敗の文面 (例外の種類で分ける。文面は `safe_error_text` を通す):
  - 接続できない: 「MT5 bridge (http://localhost:8812) に接続できません。bridge が起動しているか、URL が正しいか確認してください。[r] 再試行 / [2] 『使う予定・未準備』として進む / [u] URL を入れ直す」
  - 401/403 (https か loopback で鍵を送ったのに拒否): 「bridge の API キーが一致しません。`.env` の MT5_BRIDGE_API_KEY を確かめてください (値はここでは入力しません)。[r] 再試行 / [2] / [u]」
  - 平文 http (非 loopback) では鍵を送らない (v1.5)。`/health` が 200 で鍵の要る endpoint が 401 のときは失敗にせず `price: unverified`、reason `key_withheld_plain_http`、「bridge は応答しています。平文 http には鍵を送らないため接続の検査はここでは行いません。daemon は `.env` の鍵で接続します。」として次へ (再試行の問いは出さない。2026-10-15 裁定 (b)、v1.8)。daemon 側の未完手順 `bridge_not_connected` は記録が `planned` でも daemon の data_state が ready なら出さない。
  - 503: 「bridge は動いていますが、MT5 のサーバ時刻がまだ確定していません (bridge は確定するまで 503 を返します — `mt5_bridge/README.md` サーバ時刻オフセット節)。MT5 端末がログイン済みで、市場が開いているときに再試行してください。」
  - `mt5_connected: false`: 「bridge は動いていますが MT5 端末とつながっていません。端末のログイン・自動売買の許可を確認してください。」
- 書く予定の値: `datafeed.mt5.enabled: true`、`bridge_url`、**他の源 (`yfinance`、`twelvedata`) は全部 `enabled: false`**。`datafeed.primary` は書かない (enabled が 1 個なら導出される — 付録 A で実測)。理由: outage 設計書 (`docs/superpowers/specs/2026-09-24-outage-stop-and-backfill-design.md` 要点 1(a)) が指摘した「yfinance を true のまま MT5 を有効にする設定ミス」をウィザード経由では起こさない。
- 記録: `price: ok` + 時刻。

**分岐 2 (使う予定・未準備)**
- 表示: bridge を配置して afx とつなげるまでの手順 (下記)。最後に「この手順は `status` と起動画面にも出し続けます」。
- 尋ねる: 予定している URL (既定 `http://localhost:8812`)。
- 書く予定の値: 分岐 1 と同じ (MT5 のみ enabled)。接続試験はしない (利用者が「試す」を選んだときだけ行い、結果を記録)。
- 記録: `price: planned` (未接続)。
- **未接続のまま起動したときの afx の振る舞い (現物で確認)**:
  - サービスは起動できる。起動前検査に価格源の疎通は無い (`service.py:836-1046` の検査一覧 inventory §4.1 に価格源は無い。init の healthcheck は警告のみ `service.py:447-459`)。
  - 毎 tick、開場中なら各 (ペア × 足) の取得を試み、失敗すると次の tick に再試行する (`datafeed/ingest.py:139-147`: 失敗時 `next_probe_at = now`)。閉場中は試みない (`ingest.py:87-88`)。相手は利用者自身の bridge (既定 localhost) なので外向きの負荷にはならない。
  - 取得失敗は即 `degraded` (`datafeed/outage.py:280-292`)。`ready` 以外では取引判断 mission (cron・signal とも) が起動しない (`core/scheduler.py:370-388`)。新規発注も止まる (executor の `state_fn`、`service.py:958`)。`status` に `data: DEGRADED (…)` が出る (`commands.py:369-393`)。
  - bridge が繋がると、まだ足が 1 本も無い (watermark が無い) ので必要日数ぶんを一度に取得する (`ingest.py:106-107`: `start = now - plan.days`)。これが初回の取り込み。
  - 復帰: 建玉・未約定が無く `datafeed.outage.auto_resume_when_flat: true` (既定) なら、健全な tick が `ready_confirm_ticks` (既定 3) 回続くと自動で `ready` (`outage.py:384`)。初回は建玉が無いので自動復帰する見込み。ただし初回の 1m 足が空だった場合に `degraded` から始まる件は実機未確認 (ticket `first-run-empty-1m-starts-degraded`)。
  - 改善 mission は価格源の状態で止まらない (`core/improve_supervisor.py` と `loops/improve_loop.py` に outage・data state の参照なし — grep で確認)。履歴が無いので backtest は失敗する (段 6・§7 で表示)。
- **案内文 (bridge を配置して afx とつなげるまで)**: Wine・MT5 本体の導入は扱わない旨を先頭に書く。
  1. MT5 端末が動いてログイン済みの Windows (または Wine) 環境を用意する (この手順の範囲外)。
  2. そこにリポジトリの `mt5_bridge/` を置き、`mt5_bridge/.env.example` を `.env` に写して MT5 のログイン情報を書く。**`BRIDGE_API_KEY` を設定することを勧める** (bridge には発注系の endpoint `/order` 等があり、`BRIDGE_HOST` の既定は `0.0.0.0` — `mt5_bridge/config.py:70`、`server.py:352`。キー無しでは同じネットワークの誰でも叩ける)。`BRIDGE_HOST` は afx から届く最小の範囲にする。
  3. bridge の環境で `uv sync` → `uv run python server.py`。
  4. afx を動かすマシンから `curl <URL>/health` で `"mt5_connected": true` を確認する。
  5. bridge にキーを設定したなら、afx 側の `.env` に `MT5_BRIDGE_API_KEY=<同じ値>` を書く。
  6. `afx init` を再実行し、段 3 で「1. 使う・動いている」を選ぶ (接続試験が記録される)。
  7. 戦略の採用審査に使う履歴を取り込む: `uv run afx history import --source mt5 --symbol USDJPY --from <開始日> --to <終了日> --interval 5m`。
  - 注: `mt5_bridge/README.md` は旧プロジェクトの手順を含むため、そこに書かれた設定キー (`mt5_bridge:`) は使わないこと (§10 で別チケット)。

**分岐 3 (使わない)**
- 選択肢: yfinance (既定) / Twelve Data (API キーが要る)。
- yfinance の表示: 「yfinance は週明けの開場直後 (日曜 21:00〜23:00 UTC ごろの足が来ない) に、毎週しばらく取引判断を止めます (degraded)。既知の問題です (ticket `outage-stalled-ignores-closed-hours`)。」
- Twelve Data の表示: 「`.env` に TWELVEDATA_API_KEY が要ります。無料枠の使い方 (外向きリクエストの予算) はまだ設計されていません。」キーが無ければ選べない (起動拒否になる: `config.py:604,624`)。
- 検査: `sources.yf_quote(pairs[0])` を 1 回 (確認を取ってから。外向きリクエスト 1 回)。
- 書く予定の値: 選んだ源だけ `enabled: true`、他は `false`。加えて **`backtest.eval_source: none`** (履歴源なし。段 6)。
- 記録: `price: ok | failed` + 時刻。
- **履歴について**: 段 6 で「戦略の採用審査はできません」を表示し、`backtest.eval_source: none` を書く。

### 段 4: LLM (取引判断と改善)

- **表示の要旨**: 「取引判断 (1 足ごと) と戦略改善 (週次など) にそれぞれ LLM を割り当てます。取引判断は local か claude、改善は local / opencode / claude / codex。claude と codex はサブスク (従量課金 API ではない) を使います。」
- **尋ねること**: trade の backend → モデル、improve の backend → モデル (+ backend 固有の項目)。
- **既定**: llama-swap が応答するか (既定 URL `http://localhost:8080/v1` の `/models` を 1 回試す) で分ける。応答すれば両方 `local` を既定にする (CLAUDE.md「デフォルト両方 local」は設定の既定であり、ここは対話の既定提案)。**応答しなければ local を既定にせず、選択肢を明示して必ず選ばせる**: 「1. Claude を使う (取引判断・改善とも `claude -p`、サブスクの枠を使う) / 2. 改善だけ Codex にする (取引判断は Claude) / 3. 別ホストの llama-swap の URL を入れる / 4. 後で設定する」。「後で設定する」は `runner.*` に触れず、記録は `llm_trade: unverified`・`llm_improve: unverified` (reason `deferred_by_user`)。これは `afx check` の warning であって fatal ではない (§6.2)。段 13 は unit を設置し、起動後は degraded と未完の手順 (LLM 未検証) を表示する (完了は妨げない)。example の架空モデル名をそのまま残すか、明示的な「未設定値」を書くかは T1 / T2 / T7a で確定し、同じ task で `Settings` の検証と `afx check` の受入試験 (AC-43) を足す。改善は「ローカル LLM なら opencode が本線、local は最低線」(memory `local-llm-harness-is-opencode`) を 1 行添える。
- **検査**:
  - local / opencode: llama-swap の URL を尋ね (既定 `http://localhost:8080/v1`、userinfo は拒否)、`GET {url}/models` の一覧を番号で選ばせる (**ロードはしない**: `/models` はロード不要、`/props` と chat はロードさせる — `service.py:98-112`)。一覧が取れなければモデル名を手入力にし、記録は `unverified`。
  - opencode の `context_limit`: 「llama-swap に問い合わせると、モデルがロードされ数十秒〜数分かかることがあります。問い合わせますか」で yes のときだけ `/props` (`service.py:117-140`) を読み、値を提案。no なら手入力。`improve.llama_swap_verified` は**ウィザードは true にしない** (人が `afx improve verify-backend --backend opencode` 合格後に手で書く現行の規律、`backtest/cli.py:773-775`)。未完の手順に残す。
  - claude / codex: bin の解決 (`shutil.which` → 絶対パス)、`--version`、認証ファイルの**存在** (`service.py:210-259` の既存関数を候補値で呼ぶ)。認証ファイルは開かない・写さない。claude は `which` で見つかった絶対パス (symlink は辿らない) を `runner.claude.bin` に書く (example の既定 `claude` は裸の名前で、mission 側は裸の名前を cwd 相対に解決しないため。既に絶対パスの実行形式なら触らない。v1.7)。codex は ELF の絶対パスが要る (`service.py:210-235`) ので、見つからなければ「`runner.codex.bin` に codex の実体 (node のラッパーではない) の絶対パスを入れてください」。
  - 秘密 env (検査⑤ `service.py:277-327`): CLI backend を選んだとき、**ウィザード自身の環境**に `_API_KEY` 等を含む名前があれば「この端末の環境変数 FOO_TOKEN は CLI backend の起動を拒否させます (値は表示しません)。unset するか `.env` に移してください。unit から起動する場合は unit の環境で判定されます」(unit 側は §6 の `afx check` で判定)。
  - 実動作の確認: 運用者 shell の非隔離の `afx improve verify-backend --backend <名>` (内部の sentinel `OPERATOR_SHELL_NO_ISOLATION`、`backtest/cli.py` の `_improve_verify_backend`)。「1 回だけ LLM を実際に呼んで往復を確かめます。service の追加隔離 (seccomp / cgroup) なしの疎通確認で、隔離下で動くことの保証ではありません。claude / codex ではサブスク枠を少し使います。実行しますか [y/N]」。既定 N。結果は `llm_improve` に記録し、`ok` は疎通のみを意味する (CLI 自身も同じ断り書きを標準出力に出す)。
  - 隔離適合はウィザードでは試せない。service 起動時の admission (canary + 自己試験) が判定し、技術ログ (journal) の起動行 `improve isolation: enabled; backend=… admitted=yes|no [reason=…] fingerprint=…` に出る (`core/improve_admission.py`)。ウィザードは段 13 の起動後にこの行を探して見せ (§6.4)、見つからなければ「admission は起動後しばらくかかります。`journalctl --user -u afx | grep 'improve isolation'` で確認してください」と案内する。`admitted=no` の間、CLI backend (claude / codex / opencode) の改善 mission は隔離できないため止まる (`sandbox_unavailable` で fail closed)。backend が local のときの行は `is not a CLI backend` となり、admission の対象外。
- **local LLM が無い環境 (主シナリオ) の案内**: 前提の導入はウィザードが代行せず、導入できているかを検査して固定の文面と固定 reason で案内する。導入手順そのものは repo 内の `docs/operations/first-run-acceptance.md` の前提節 (T14) に置き、ウィザードの文面はそこへの導線 (節名) を添える。
  1. **Node の確認** (claude / codex が Node を要するとき): `node --version` が取れなければ「Node.js が見つかりません (`bin_not_found`)。claude / codex の導入に必要です。手順は docs/operations/first-run-acceptance.md の『前提: CLI の導入』を見てください」。
  2. **Claude CLI の導入確認**: `shutil.which("claude")` と `claude --version`。無ければ「claude が見つかりません (`bin_not_found`)。[r] 導入後に再試行 / [d] 後で設定する」。
  3. **ログインの確認**: 認証ファイルの存在 (開かない)。無ければ「claude にログインしていません (`credentials_missing`)。端末で `claude` を起動してログインしてから再試行してください。ここでは入力しません」。
  4. **`claude -p` の最小確認 (消費確認つき)**: 取引判断または改善に Claude を選んだとき、`--version` と認証ファイルの存在の確認のあとで、ウィザードが確認を出す: 「trade 用の `claude -p` を 1 往復で試験します。サブスクの枠を少し使います。送りますか [y/N]」(既定 N)。同意なら trade 用の呼び出しを 1 往復だけ行い、結果を `llm_trade` に `ok` / `failed` と固定 reason で記録する。同意しなければ送らず `llm_trade: unverified`、reason `user_declined_probe` を記録し、未完の手順に出す。改善側の実往復は上の「実動作の確認」(`verify-backend`、既定 N) が別に持つ。手順書には手動での `claude -p "ok だけ答えて"` も残す (ウィザードを経由しない場合の確認用)。
  5. **trade / improve で Claude を選ぶ操作**: 上の選択肢 1 を選ぶと `runner.trade.backend: claude`、`runner.improve.backend: claude` を書く (モデル名は example の `model` 行を claude 用の既定に置き換える。値は実装 task で現行の対応表から決める)。期待する表示は「取引判断: claude (`claude -p`、サブスク) / 改善: claude (サブスク)。1 回の改善 mission は枠を使います」。
  6. **改善を Codex にする分岐** (選択肢 2): `codex` の実体 (node のラッパーではなく ELF) の絶対パスの確認、`--version`、認証ファイルの存在。見つからなければ `bin_not_found`、ログインが無ければ `credentials_missing`、path が ELF でなければ `runner.codex.bin` の入れ方を案内 (既存の文面)。
  7. **別ホストの llama-swap** (選択肢 3): URL を入れ、`GET {url}/models` を 1 回試す。成功 = モデル一覧から選ぶ。失敗 = 接続不可 (`connect_error`)、401/403 (`auth_unsupported`: 「この URL は認証を要求しています。ウィザードは認証つきの llama-swap に対応していません。認証不要の URL を使うか、後で設定してください」)、一覧が空 (`model_not_found`)。認証を要しない前提を表示する。URL に userinfo があれば拒否 (既存)。
  8. **未導入・未ログインでも完了する**: 上の 2〜3・6 で解決しなくても「後で設定する」または「未確認のまま進む」で段 9 に進め、記録に `bin_not_found` / `credentials_missing` が残り、未完の手順が手順書の節名つきで出し続ける。これらは `afx check` の warning なので unit は設置され、起動後は degraded と未完の手順を表示する (§6.2、§6.4)。unit の起動が CLI の不在で拒否されて再起動ループにならないよう、warning に分類した条件は service 側の起動前検査でも起動拒否にせず degraded にする (T7a。fatal / warning の分類は `afx check` と service で同じ関数を使う)。
- **失敗時**: 「未確認のまま進む」を選べる (B-6 b)。記録は `llm_trade: unverified | failed`、`llm_improve: …`。
- **書く予定の値**: `runner.trade.{backend,model}`、`runner.improve.{backend,model}`、`llama_swap.base_url`、選んだ backend の `runner.<backend>.*` だけ (`runner.claude.bin` は上記の絶対パス)。使わない backend の値は触らない (`runner.codex` は使わなくてもキー必須でプレースホルダのまま検査をすり抜ける — inventory C-9。これは現状維持)。

### 段 5: 判断足 (トレード足)

- **尋ねること**: 判断足を番号で選ぶ (v1.8)。選べる足は既知の足 (`INTERVAL_MIN`) のうち、判断の上限より長く、かつ 240 分未満 (4h 以上は保存が 1h に正規化される派生専用で判断足にできない、`config._decision_timeframe_is_stored`) のもの = `15m` / `30m` / `1h` (既定 `1h`、再実行は現在値)。選べない足 (`1m` `5m` `4h` `1d`) も一覧に出して理由を添え、番号は振らない。手入力は無し。(旧: 選択肢は `15m` / `1h` (config のエラー文が勧める 2 値 `config.py:236-240`)。既知の足は `1m 5m 15m 30m 1h 4h 1d` (`datafeed/sources.py:16-17`) で 4h 以上は拒否されるが、`30m` は example の `intervals` に無く、判断足として使えるかは未確認なので選択肢に出さない。`1m`・`5m` は次の検査で落ちることを示すために選択肢には出し、選ぶと理由を表示する。
- **既定**: `1h` (example)。理由: 1 回の判断の上限時間と余裕があり、LLM の呼び出し回数も抑えられる。
- **検査**: 段 4 で選んだ backend と `llama_swap.timeout_sec` で worker 1 回の上限 (Cw) を `core/mission_ceiling` で計算し (`service.py:680-707` と同じ式。`_check_mission_ceilings` は activity を要るので純粋関数を直接使う — inventory §3.4)、判断足の幅以上なら「判断足 5m (300 秒) は、1 回の判断の上限 405 秒より短いため使えません。15m 以上を選ぶか、llama_swap.timeout_sec を下げてください」。`plugin.signal_lease_min` も同時に検算。
- **再実行で変えるとき**: 「判断足を変えると、既に配備した strategy plugin の足 (config.yaml の timeframe) と食い違うことがあります。食い違うと signal が静かに効かなくなります (`service.py:658-668`、技術ログの WARNING のみ)。」配備済み strategy の足を一覧して食い違いを表示。cron cursor・保存済み判断の足タグへの影響は未確認 (inventory C-6)。

### 段 6: 履歴と backtest

- **表示の要旨**: 「改善ループが作った戦略を採用するかどうかは、過去の価格での試験 (backtest) の成績で判断します。そのための履歴データの取り込み元を決めます。」
- **段 3 の分岐ごとの扱い**:
  - 分岐 1・2 (MT5): 履歴源 `mt5` (`backtest.eval_source: mt5` を書く)、基底足の既定 `5m`。理由: MT5 の 1m の深さは約 3 ヶ月で、holdout 3 ヶ月と合わせると in-sample が取れない (inventory §2.3、ticket `backtest-base-interval`)。`1m` を選んだら「MT5 の 1m は約 3 ヶ月分で、holdout 3 ヶ月を除くと試験期間が残りません」と警告。取り込みの手順 (段 3 案内の 7) を表示。分岐 2 では「bridge が繋がってから」。
  - 分岐 3 (使わない): 「MT5 bridge が無いと、戦略の採用審査に使う履歴を取り込めません。この状態では**改善ループは戦略を作れても採用審査を通せません**。指標の導入はできます (指標の審査は履歴を使いません — `plugin/approval.py:220-221`)。」**必ず `backtest.eval_source: none` を書く** (設定でも「履歴源なし」を表す。example の `dukascopy` を残さない)。Dukascopy は選択肢に出さず、勧めない: 「Dukascopy は無料公開のサービスで、取り込みは 1 秒間隔で最大 500 リクエスト/回を投げます。相手に負荷をかけ、遮断されることがあります。使う場合は自分で範囲を絞って `afx history import --source dukascopy` を打ち、`backtest.eval_source` を自分で書き換えてください。」
    - `none` の振る舞い (実装 task T13): strategy の採用審査 (gate) は backtest を実行せず固定理由 `history_source_unconfigured` で止まる (indicator・signal の審査は履歴を使わないので影響しない)。`status` / splash は「履歴源が設定されていません」と、MT5 bridge をつなげて `backtest.eval_source: mt5` にする手順、または別の対応履歴源を設定する手順を出す。`afx history import` は `--source` を省略されても settings の `none` から Dukascopy などを推測しない (`--source` 必須の扱いは実装 task で現行を確認する)。`config/settings.yaml.example` の `eval_source` の既定も `none` にする。`HistoryDataset` の source 検査は、import 可能な源の集合に `none` を足した集合を受ける (import の選択肢には `none` を足さない)。
- **holdout**: 表示のみ (「3 ヶ月。変更は人の審査が要る項目です」)。尋ねない。
- **ウィザードが取り込みを実行しない理由**: 取り込みは長時間 (件数ぶんの HTTP) で、bridge の準備状況にも依存し、範囲は利用者が決めること。ウィザードは手順と「履歴なし」の表示まで。

### 段 7: 改善周期と最初の 1 本 (B-8)

- **尋ねること**: 周期 (weekly / daily)、時刻 (weekly は `Sat 03:00` 形式、daily は `03:00`)、最初の 1 本をいつ走らせるか。
- **既定**: weekly `Sat 03:00` (example)、「次の予定まで待つ」。
- **表示**: 入力値から、表示 TZ での次回予定を日時で示す。計算は supervisor と同じ `latest_scheduled_occurrence` / `period_key_of` (`core/scheduler.py:1414-1470`) を使い、別実装しない。例 (Asia/Tokyo、weekly `Sat 03:00`):
  - 2026-10-02 (金) 15:00 JST に完了 → 「次回は 2026-10-03 (土) 03:00 JST」。直近の予定は 2026-09-26 (土) 03:00 で、これは「消費済み」にする。
  - 2026-10-03 (土) 10:00 JST に完了 → 直近 2026-10-03 03:00 の期を消費済みにし「次回は 2026-10-10 (土) 03:00 JST」。
- **「最初の 1 本」の選択肢**:
  1. 次の予定まで待つ (既定) — 起動直後には走らない。その間に方針文と最初の課題を入れられる。
  2. サービス起動の直後に 1 本走らせる — 現行と同じ挙動。
- **B-8 の決定: (b) を採る。ウィザードが「今期は消費済み」の印を DB に置く**。
  - 仕組み: 改善 supervisor は毎 tick、直近の予定時刻の期 (`period_key`) の wave 行が無ければ作って起動する (`core/improve_supervisor.py:96-107`、`store/improve_waves.py:15-38` の `INSERT OR IGNORE`)。ウィザードは段 10 で、直近の予定の期について **slot を持たない wave 行 (`expected=0`)** を入れる。supervisor の `INSERT OR IGNORE` は 0 行になり起動しない。次の予定時刻を過ぎると期が変わり、通常どおり走る。
  - 根拠 1: supervisor のコードを変えない。改善の起動条件は資金保護ではないが、すでに wave/slot の CAS 規律で固められた所で、変えると回帰の範囲が広い。
  - 根拠 2: `expected=0` は既存経路では作られない (`create_wave_and_slots` は `expected == 0` で何も書かない `improve_waves.py:22-23`) ので、「ウィザードが消費済みにした期」と区別できる。slot を作る形 (`expected>=1` の reserved slot) は、起動時に `recover_stale_slots` が `failed` に変える (`improve_waves.py:150-168`) ため、実行していない期が「失敗」として履歴に残り、読み手を誤らせる。だから採らない。
  - 根拠 3: (c) (catch-up 条件そのものの見直し) は、ウィザードと無関係な「停止中に予定を跨いだ再起動」の挙動も変える。そちらは ticket `improve-catchup-runs-at-startup` (裁定待ち) で別に決めるべきで、ウィザードの範囲に入れない。
  - 確認が要る点: `improve_waves` 行を読むのは現物で `INSERT OR IGNORE` のみ (`grep improve_waves src/` で SELECT 無し)。slot 数を読む `_compute_partition_hint` (`loops/improve_loop.py:866-873`) は claim 済みの slot を持つ mission の中だけで呼ばれる。`expected=0` の行が他の集計・表示に出ないことを実装 task で pin する。
  - 再実行で周期・時刻を変えたとき: 新しい設定での直近の期にも同じ印を置く (利用者が「今すぐ」を選んだ場合を除く)。周期を変えると期のキーが変わり (`weekly` は ISO 週、`daily` は日付)、印が無いと再起動直後に走るため (inventory §6.3)。
  - 制限 (残るリスク): ウィザード完了から実際のサービス起動までの間に次の予定時刻を跨ぐと、起動直後に 1 本走る。これは「予定どおりの 1 本」なので仕様とする。
- **CLI backend のとき**: 「1 回の改善 mission はサブスク枠を使います」を表示。1 回あたりの目安量は未計測 (inventory 付録) なので数値は出さない。

### 段 8: 操作 API と `.env` (B-13)

- **尋ねること**: 「afx を常駐サービス (systemd user unit) として動かしますか [Y/n]」(v1.8。旧: 「操作 API を有効にしますか」)。利用者が決めるのは常駐か対話シェルかで、操作 API はその結果: Y → `api.enabled: true` + 段 13 で unit を置く (段 13 は常駐の意思を再質問せず設置内容の確認だけ)、n → `api.enabled: false` + 段 13 は unit を置かず「手動起動: `uv run afx`。常駐に切り替えるときは `afx init` を再実行」の案内だけ (記録 `unit: not_installed` / reason `declined`、`start: skipped`)。再実行の既定は現在値 (`api.enabled` と unit の有無)。
- **表示**: 「操作 API は同じマシンの UNIX ソケット (`data/run/api.sock`) だけで受け付け、TCP では開きません。常駐 (daemon) で動かすときの承認・方針文の追加・改善の起動は `afx ctl` から行うので、有効にすることを勧めます。無効にすると `afx ctl` と、段 13 の bot / web / notify は使えません。また `afx ctl status` に未完の手順を出すこともできなくなり、案内は起動画面とこのウィザードの表示だけになります。」
- **既定**: 有効 (`api.enabled: true`)。再実行は現在値。example の `api.enabled` 既定は T8 で `true` に変え、「seccomp が入るまで」のコメントを消す。それまでの example 由来の `false` は、この段が `true` に書き換える (P16)。鍵は service が起動時に補完する (`ops.keys.ensure_initialized`) ので、ウィザードは鍵を作らない。外から見る手段を選んだときだけ、`afx ops enable` が停止中に鍵を初期化する。
- **通知の扱い**: Discord への通知・承認は段 13 の外から見る手段 (bot / notify) に一本化し、この段では尋ねない。旧設定 `discord.enabled` (`.env` の `DISCORD_WEBHOOK_URL` へ送る簡易通知、`core/notifier.py`、`service.py` が配線) は、ウィザードは尋ねず値も変えず、既定 `false` のままにする。この簡易通知の廃止は別チケット `legacy-discord-webhook-notify-removal` (優先度 低) で扱う。
- **`.env` の確認**: これまでの回答から必要なキー名を決め、**存在だけ**確認する (値は読み出して表示しない。行頭 `KEY=` の後が空でないか、または既に環境変数にあるか)。
  - 既存の設定で `discord.enabled: true` のとき → `DISCORD_WEBHOOK_URL`。無いと**通知は黙って無効**になる (`core/notifier.py:13`: `enabled and bool(webhook_url)`) ので未完の手順に出す。
  - Twelve Data を源にした → `TWELVEDATA_API_KEY` (無いと起動拒否)。
  - MT5 を源にし、段 3 で「bridge に API キーを設定した」→ `MT5_BRIDGE_API_KEY`。「分からない」は「キーを設定した場合だけ必要」と表示。
  - 表示例: 「.env に必要なキー: MT5_BRIDGE_API_KEY (あり)、TWELVEDATA_API_KEY (なし)」。
  - Discord token と通知 webhook URL は `.env` に書かない。`afx ops enable` が feature 専用の secret file に置く (段 13)。
  - `.env` が無ければ作らない (B-13)。`.gitignore:13` で除外済みであることだけ表示。
- `service.secret_env_allowlist` は**ウィザードが足さない** (名前だけで検査⑤を外すので、人の確認が要る — inventory C-3)。

### 段 9: 要約と最終確認

- **表示** (v1.8): 利用者の言葉で要約する — 価格源 (源・URL・鍵の有無・検査結果を文で)、取引判断と改善 (backend とモデル)、判断足・backtest の基底足・履歴源、改善周期と次回と最初の 1 本、常駐 (段 8 の答え)、作るもの (settings.yaml、DB、状態、確認記録)、このあと尋ねること (標準指標、方針文と最初の課題、常駐化と外から見る手段)。設定キー名と内部語 (確定 1 / 確定 2、planned 等) は出さない。`d` で従来の「キー: 現在値 → 新しい値」(行単位の diff と同じ。版が古ければ migration が挿入・変更する行も同じ表に) を表示して問いに戻る。
- **再実行で「注意が要る項目」を変えた場合** (inventory §8.2: `paper.starting_balance`・`account_currency`・`datafeed` の源・`decision_timeframes`・`backtest.*`): 項目ごとに帰結の 1 文を出し、`yes` の打ち込みを求める (Enter では通らない)。建玉・未約定があるときは「建玉が N 件あります。これらの項目を変える前に建玉を閉じることを勧めます」。
- **検査**: 書き換え後の全体を G1〜G3 (付録 A) + 起動前検査のうち設定だけで決まるもの (cache retention `service.py:553-582`、mission ceiling `service.py:680-723`)。失敗は該当の段へ戻る。
- **尋ねること**: 「この内容で書き込みます [Y/n] (d = settings.yaml の差分を見る)」(v1.8。旧: 「書き込みますか [y/N]」)。n なら何も書かずに終了 (lock を解放)。

### 段 10: 確定 1 (設定・DB・状態・記録)

設定 (YAML)・DB (SQLite)・状態 (JSON)・ログは別々のストレージで、1 つの transaction にはできない。そこで、途中で落ちても同じ `afx init` の再実行で収束する**回復可能な commit protocol** として確定する。順序は固定:

1. **marker を置く**: `data/state/setup_in_progress` (`phase=commit\nstarted_at=<UTC ISO>\nsettings_sha256=<新しい settings.yaml 全文の sha256 hex>\n`。handoff は `phase=handoff\nstarted_at=<UTC ISO>\n`。読む側は `store/setup_marker.py::read_setup_marker`) を、一時ファイルに書く → fsync → rename → 親 dir の fsync で置く。以後、サービスは起動時にこの marker を見て、専用の終了コード 76 (`setup_in_progress`。`afx ops enable` の enroll 中の 75 とは別) で即終了する (§4.3)。
2. `config/settings.yaml`: 初回は example を写したものに回答を当てた全文、再実行は既存ファイルに回答を当てた全文 (版が古ければ migration も同じ全文に含める。§4.4) を、一時ファイルに書く → fsync → `os.replace` → 親 dir の fsync。再実行では置き換え前の内容を `config/settings.yaml.bak-<UTC時刻>` に残す。ただし marker の sha256 と既存ファイルが一致する (= 前回の続き) ときは bak を作り直さず、最初の bak を保つ。
3. `logs/` と技術ログの設定。
4. DB: `connect` + `init_db` (旧スキーマの移行を含む。lock 保持下なのでサービスと同時に走らない)。続けて**1 つの transaction (`BEGIN IMMEDIATE`) で**、基本ニュースソースの登録 (`seed_default_sources`、冪等) と期の消費印 (段 7 で「待つ」を選んだとき、`INSERT OR IGNORE`) を行う。失敗したら transaction は rollback され、次に進まない。`init_db` の移行が `agentic.db.bak-*` を作ってから rebuild する現行の挙動は変えない。
5. 確認記録 `data/state/setup.json` (§7) を原子的置換 (一時ファイル → fsync → rename → 親 dir の fsync) で書く。書けなければ失敗として扱い (警告で流さない)、marker を残す。
6. 状態 `data/state/app_state.json`: `initialized=true`。初回 (`initialized` が偽だった) のときだけ `mode=learning, autopilot=off`。kill switch の latch には触れない。再実行では `initialized` 以外に触れない (現行 `run_init` は毎回 mode/autopilot を戻す `service.py:470-471` — この挙動は `--non-interactive` だけに残す)。
7. activity `init_completed` (現行どおり。失敗は警告)。
8. **marker を消す** (unlink → 親 dir の fsync)。消えるまで、サービスは起動できない。消せなかったときは rc=1 で、再実行で消す旨を表示する。

どこで失敗・中断しても表示するのは、「何が書かれ、何が書かれていないか、次に何をするか」。次にすることは常に同じ: `afx init` の再実行 (marker が残っているので、その間サービスは先に進まない)。各中断点の実物の状態と再開地点は §4.3 の表。

- 表示: 「設定を書き込みました。ここから先 (指標・方針文・常駐化) は 1 つずつ確定し、途中でやめても書いた所までが残ります。」

### 段 11: 標準指標の採用 (B-9 a+c)

- Landlock が無ければ説明だけで飛ばす。改善隔離の ABI が足りないとき (段 0) は周期を尋ねたうえで「この kernel では改善は走りません」を 1 行添える。
- **表示**: 9 本の一覧 (sma, ema, rsi, macd, bollinger, atr, adx, stochastic, ichimoku — `docs/examples/plugins/`)。各行に「何を計算するか」1 行と、既に同名が配備済みかどうか。「指標だけでは取引しません。改善ループが戦略を作るときの材料になります。」
- **尋ねること** (v1.8): 一覧を見せて「標準指標 N 本を登録します [Y/n] (e = 外す番号を選ぶ)」の 1 問。既定 Y (人の確認は 1 問で記録される。`decided_by` の値は変えないが文面には出さない)。`e` で外す番号の入力に入る。再実行で全部配備済みなら「標準指標 N 本は配備済みです (変更なし)」の 1 行だけで問いも警告の繰り返しも出さない。一部だけ配備済みなら未配備の本数で問う。(旧: 外したい番号を入力させ、続けて「この N 本を採用します…(decided_by=human_cli)…[y/N]」既定 N)
- 同名が配備済み: 既定で外す。「rsi は既に配備されています。新しい版へ切り替えると、rsi を使う strategy の固定が外れることがあります (手順書 `docs/operations/indicator-initial-set-deploy-2026-09-19.md` の (0))。」
- **実行**: 1 本ずつ既存の bless 経路 (`plugin/switch.py` `bless_candidate`、`--from _human` と同じ候補置き場) を通す。gate (AST 検査・pytest・hash 照合)・journal・ledger を迂回しない。`plugins/` に直接コピーしない。進捗「3/9 macd: 審査中 (pytest、最長 300 秒)」。1 本の失敗は他に影響しない。
- 失敗の文面: gate 前の失敗 = 「bollinger: 審査に通りませんでした (pytest 不合格)。何も配備されていません。後で `uv run afx plugin bless bollinger --from _human` でやり直せます。」/ gate 後に止まった = 「承認 #12 (macd) が途中で止まりました。サービス起動後に `afx ctl --as approver approval retry 12` を実行してください (端末での確認があります)。」(plugin の CLI に retry は無く、操作 API の `afx ctl` にある。未完の手順に出す)
- 候補置き場 (`plugins/_human/<名>`) の後始末は手順書 (4) と同じく成功後に消す。
- 反映は次回起動時 (`service.py:1004`)。

### 段 12: 方針文と最初の課題 (B-10 b)

- **案内** (v1.8): 各問いの前に用途 1 行 (方針文 = 取引判断と改善の全 mission に注入される運用者の方針、課題 = 改善ループが最初に取り組む 1 件) と例文 (方針 3 行、課題 1 件) を出し、「後から `afx ctl policy add` / `afx ctl improve add` で足せる、空 Enter で飛ばせる」を添える。`e` で例文をそのまま入力にする。例文は雛形であり、方針の実文言は利用者が決める (2026-09-12 の方針は変えない)。

- **方針文**: 「取引判断と改善の両方に、あなたの方針として毎回渡されます (末尾 4000 字)。後から `afx ctl policy add <文>` で足せます。」自由文を 0〜数行受け付ける (空でよい)。**雛形・例文は出さない** (ticket `legacy-oc-example-leak`: 例文がそのまま実在化した前歴)。書き方は `policy add` と同じ `- <text>` の追記 (`commands.py:315-326`)。ファイルが無ければ作る。
- **最初の課題**: 「改善ループに最初に考えてほしいこと (どんな戦略を作らせたいか) を自然文で書いてください。空でも構いません。」0〜数件。`store.backlog.add(source="user")` で入れる。プレースホルダ検査は `commands.py:41-57,248-256` と共通化して使う。
- 書き込みは 1 件ずつ (方針文はファイル追記、課題は DB 1 行)。
- 注: 種まきより古い observation が先に選ばれる件 (ticket `seed-priority`、裁定待ち) は初回 (backlog 空) では起きない。

### 段 13: 常駐化と外から見る手段 (§6 で詳述)

順序は固定する。afx が停止している間に済ませ、起動は最後にする。ウィザードはこの段の最後まで setup.lock を保持する。

1. **常駐化の有無**: 段 8 の答え (常駐 / 対話シェル) を使い、ここでは再質問しない (v1.8)。置かないなら「手で起動するには `uv run afx` (端末では対話シェルが開きます)。常駐に切り替えるときは `afx init` を再実行」と表示し、手順 2〜6 を飛ばして段 14 へ (外から見る手段は unit を前提にするので尋ねない)。`systemctl --user` が使えないときの reason は、`XDG_RUNTIME_DIR` が無ければ `user_session_missing` (「ユーザーセッションが見えません。`su` ではなく直接ログインして `afx init` を再実行」)、あれば `systemd_unavailable`。
2. **unit の設置**: 内容を表示 → 置くか → `daemon-reload`。この時点では起動も `enable` もしない。setup.lock と instance.lock の両方を保持している。
3. **外から見る手段**: 「外から見る / 操作する手段 (Discord bot / 読み取り専用 web / 通知) を今設定しますか」(既定 = あとで)。「あとで」なら手順 4・5 を飛ばして 6 へ。
4. **引き渡し (handoff)**: (a) marker `data/state/setup_in_progress` を `phase=handoff` で置く (サービスの起動を 76 で塞ぐ)、(b) instance.lock を解放する、(c) setup.lock は保持したまま。この順序が崩れない限り、instance.lock の隙間に別の起動が入っても marker が止める。以後ウィザードは設定・DB・`app_state.json` を書かない (§4.2)。
5. 選ばれた feature ごとに既存の `afx ops enable <feature>` と同じ処理を、**保持済みの setup lease を渡して**呼ぶ (§6.3)。質問 (token・guild・channel・承認者 allowlist、web の port、通知の webhook URL)、`afx-ops probe` による疎通確認、再質問、鍵の初期化、状態機械は enable 側が持ち、端末入力も enable 側が直接受ける。ウィザードは質問を足さず、値を受け取らず、戻り値 `EnableResult(rc, stage, reason_code)` だけを読む。afx は停止中なので enable は afx を起こさない。失敗しても完了は妨げず、確認記録に `ops.<feature>` の結果と reason を残し、`afx ops enable <feature>` の再実行を案内する。全 feature の処理が終わったら marker を消す (unlink → 親 dir の fsync)。
6. **linger、有効化と起動**: (a) linger: `loginctl show-user $USER -p Linger` を読む。`no` なら「ログアウトすると afx も止まります。`loginctl enable-linger $USER` を実行しますか [y/N]」(既定 N)。`y` のときだけウィザードが実行し、成功を `Linger=yes` の再読込で確かめてから、確認記録の `linger` に所有記録 `{state: owned_by_wizard, at}` を原子的置換で残す (実行より後に記録するので、落ちても「記録の無い有効化」になるだけで、リセットは触らない側に倒れる)。実行に失敗した (管理者権限が要る環境など) ときは `{state: failed, reason_code}` を残し、手で実行する案内を出して続行する。断られたときは `{state: declined}` を残して続行する (同文の案内は直後に繰り返さず、段 14 の未完手順 `linger_off` に 1 回だけ出す。v1.8)。最初から `yes` だったときは `{state: already_enabled}` で、ウィザードは何も変えず所有も主張しない。(b) `enable` (ログイン時に自動起動) と今すぐの `start` をそれぞれ確認する。`start` の直前に instance.lock を非ブロッキングで取って即解放し (別の afx が既に動いていないことの最終確認。取れなければ `start` せず理由を表示する)、`systemctl --user start` を呼ぶ。起動後の確認は §6.4 の 3 段階。確認が終わったら setup.lock を解放する。

「あとで」のとき ops 用の unit と `Wants=` は置かれない (afx-ops spec §7、全 off は無配置)。

### 段 14: 次の一歩

- 表示:
  - 起動のしかた: unit なら `systemctl --user start afx` (状態は `systemctl --user status afx`)、手動なら `uv run afx`。
  - 操作は `afx ctl`: `afx ctl status` で稼働状態と未完の手順、`afx ctl approval list`、`afx ctl backlog add <案>`、`afx ctl policy add <文>`。
  - 改善候補の承認は二通り。Discord bot を有効にしたなら、bot の承認依頼で二段操作 (claim → confirm)。もう一つは `afx ctl --as approver approve <id>` (approver 鍵、端末での確認あり)。
  - ログの場所: `logs/agentic.log`、`logs/activity.log`、unit なら `journalctl --user -u afx`。
  - 設定を変えるときは `afx init` の再実行 (稼働中は先に `systemctl --user stop afx`)。
- 最後に未完の手順の一覧 (§7) を 1 回表示。

### 3.1 途中で止めたとき

| 止めた所 | 残るもの | 再実行で始まる所 |
|---|---|---|
| 段 0〜9 (`q`、Ctrl-C、端末切断) | `data/` 配下の空の lock ファイル 2 つのみ | 段 0 から。既定値は従前の settings.yaml (無ければ example) |
| 段 10 の途中 | marker と、§4.3 の表の中断点に応じた実物 | 段 0 から (marker を見て続きと表示)。settings.yaml が新しければその値が既定になり、同じ回答で確定すれば収束する。marker がある間はサービスが起動しない |
| 段 11 の途中 | 完了した指標は配備済み。途中の 1 本は gate 前なら何も残らず、gate 後なら journal が残る。確定 1 は完了済み (marker 無し) | 段 0 から。段 11 では配備済みの指標が「配備済み」と表示され、既定で外れる |
| 段 12 の途中 | 書いた件まで | 同上。段 12 は既存の方針文の文字数と backlog 件数を表示してから尋ねる |
| 段 13 の途中 | 置いた unit、`enable` 済みの feature (afx-ops の stage file が持つ)、handoff の marker (引き渡しの途中なら) | 同上。unit が同一なら「設置済み」と表示し、handoff の marker があれば段 13 の手順 3 から再開する。ops は `afx ops status <feature>` の stage を見て同じ enable の再実行で続ける。再開地点の表は §4.3 |

集める層での中断は「確定していないので何も変えていません」を表示して rc=130 (Ctrl-C) / rc=1 (`q`)。

---

## 4. 再実行 (B-11) と稼働中のサービスとの排他 (B-12)

### 4.1 再実行

- 入口は同じ `afx init`。既存 settings.yaml があれば「再実行」とみなし、各段の既定値を現在値にする。答えなかった項目・尋ねない項目の行は触らない (B-2、付録 A)。
- 段 1 は 1 行に短縮。段 3・4 の接続試験は既定で再実行する (記録を更新するため)。
- 「注意が要る項目」を変えるときは段 9 の確認 (帰結の文 + `yes` の打ち込み)。
- 個別変更コマンド (`afx config set` 等) は作らない (B-11 の裁定)。手で編集した人向けに、設定だけを検査する `afx check` (§6、task T7a) を足す。これは unit 有効化前の検査と兼用。
- 設定を変えずに「最初からやり直す」ための独立スクリプト `scripts/first-run-reset.sh` は別枠 (§4.5)。再実行 (答えた行だけ変える) とは別物で、当 instance の設置した unit・鍵・設定・データを取り除く。`afx` のコマンドには入れない。
- `--non-interactive`: 従来の init と同じ (無ければ example を写す・検証・DB・ニュースソース・価格源と llama-swap の確認は警告のみ・`initialized=true` と mode/autopilot の初期化)。加えて確認記録を `mode: non-interactive` で書き、外部確認の結果は警告と同じ内容で `unverified | failed | ok` を残す。`--config <file>` を受け付け、渡されたファイルを settings.yaml に置く (既存があれば上書きせず rc=1)。

### 4.2 稼働中のサービスとの排他と、lock の引き渡し

- **lock は 2 つで、取る順序は `data/ops/setup.lock → data/instance.lock` に統一する**。`afx ops enable` が既に setup.lock を先に取り、その後 instance.lock を取る (`ops_setup/interactive.py` の `_setup_lock`、`ops_setup/stages.py` の `_lock`) ので、ウィザードもこの順序に合わせる。逆順で取る経路は作らない。
- 段 0 で setup.lock、instance.lock の順に取る。setup.lock が取れなければ、別の `afx init` か `afx ops enable` が動いているので拒否。instance.lock が取れなければ稼働中のサービス (または手動起動) があるので拒否 (B-12 a)。サービス (`build_app` `service.py:853`) も同じ instance.lock を取るので、ウィザード中にサービスが起動しても、サービス側が `InstanceAlreadyRunning` で止まる。
- `afx init` は setup.lock を**ウィザード終了まで保持する**。`afx ops enable` (対話 CLI の経路) を同時に打つと setup.lock が取れず固定 reason で止まる。
- **引き渡し (handoff)** は、段 13 で外から見る手段を選んだときだけ行う。instance.lock を保持したまま解放すると (1) enable が instance.lock を自分で取る (`ops_setup/stages.py` の `_lock`、`afx keys init` も同じ lock を非ブロッキングで取る) ので失敗し、(2) かといって先に解放すると、解放から enable が lock を取るまでの隙間に別の `afx init` やサービス起動が入り得る (入ると enable がそれを停止し、`afx_before_stop=active` として途中で再起動し得る)。そこで次の順序にする。
  1. marker `data/state/setup_in_progress` を `phase=handoff` で置く。サービスは起動時にこの marker を見て 76 で即終了する (instance.lock の取得結果にかかわらず。検査は §4.3 の起動ガードが、設定の読み込みと instance.lock の取得より前に行う)。
  2. instance.lock を解放する。
  3. setup.lock は保持したまま、保持済みの lease を `interactive_enable` に渡す。enable は自分で setup.lock を取り直さず (取り直すと自分自身に塞がれる)、lease が同じ root のものでまだ保持されていることを確かめてから進む。enable は instance.lock を自分で取る。
  4. 全 feature の処理が終わったら marker を消す。
  5. `start` の直前に instance.lock を非ブロッキングで取って即解放し、取れれば `systemctl --user start` を呼ぶ。
  引き渡しの間 (手順 1〜4)、ウィザードは設定・DB・`app_state.json` を書かない。許すのは確認記録 `setup.json` の原子的置換だけ (setup.lock の保持下)。
- 手順 5 の最終確認と `start` の間には、別の手動起動が入る隙間が残る (lock を保持したまま systemd に `start` させる手段が無い)。入ったときの帰結は、systemd 側の起動が `InstanceAlreadyRunning` で失敗し、`Restart=on-failure` で再試行されること。実害は journal の失敗行で、手動起動を止めれば収束する。受容する残余リスクとして §10 に載せる (U-11)。
- 現行の `run_init` は lock を取らない (`service.py:427-431` のコメントは「停止中に実行すること」の前提のみ)。`--non-interactive` にも同じ 2 つの lock と §4.3 の marker を入れる (稼働中の DB に `init_db` と state 更新をしないため)。作者の環境で稼働中に `--non-interactive` を打つ運用は無い前提 (未確認。打っていたなら挙動が変わる)。
- **競合試験**: (a) 別の `afx init` を同時に起動 (後発が setup.lock で rc=2)、(b) ウィザード中に手動で `main.py` を起動 (instance.lock で止まる)、(c) handoff の間に systemd の `start` / 手動起動 (marker で 76、`Restart` されない)、(d) handoff の間に別の `afx ops enable` (setup.lock で固定 reason)、(e) 各 crash 点 (§4.3) で再実行。

### 4.3 確定 1 と引き渡しの crash matrix

**起動ガード (サービス側)**: `run_service` の root 解決の直後、`ensure_initialized`・`.env` の読み込み・`load_settings` のどれよりも前に、2 つの marker を 1 箇所で判定する。判定は固定の順序で、(1) `data/ops/enroll_in_progress` (`afx ops enable` の enroll 中) が在れば終了コード 75 (`enroll_in_progress`)、(2) そうでなく `data/state/setup_in_progress` が在れば終了コード 76 (`setup_in_progress`)。両方在るときは 75 を先に返す。判定は marker の**内容を読まず**、存在だけで行う: `os.lstat` 相当で、`ENOENT` 以外の失敗 (権限、`data/state` が読めない、壊れた symlink など) は「在る」とみなして止まる (fail closed。`os.path.lexists` は例外を握りつぶして偽を返すので使わない)。この位置に置く理由: marker が在るのは settings・DB・状態が新旧混在しうる間なので、混在した settings の検証失敗 (例外終了) が 76 より先に起きると、`RestartPreventExitStatus` が効かず 30 秒ごとの再起動ループになる。検査が設定の読み込みより前にあれば、settings.yaml が壊れていても、新旧の schema が混ざっていても、同じ 76 で止まる。`build_app` の中にある既存の 75 の判定 (lock の取得時) は残し、同じ 2 つの marker の再確認を instance.lock の取得の**前**と**直後**に置く (早期ガードと lock の取得の間に marker が置かれる競合を塞ぐ。ここで見つけたときも、同じ終了コードに変換して `run_service` が返す)。unit は `RestartPreventExitStatus=75 76` を持つ (§6.1)。壊れた印でもサービスは止まる。実装は task T10。

**確定 1 (段 10) の中断点**

| 落ちた点 | 実物の状態 | サービス | 再実行の再開地点 |
|---|---|---|---|
| marker を置く前 (段 0〜9) | lock ファイルのみ | 従前どおり (`initialized` が偽なら起動ガード rc=2) | 段 0 から |
| marker 後、settings の置換前 | marker (新 sha)、settings は旧 | 76 | 段 0 から。既定は旧 settings。確定で marker を置き直す |
| settings の置換後 (replace は原子的なので新旧のどちらか) | settings 新、DB と記録は旧 | 76 | 段 0 から。既定は新 settings なので同じ回答で確定すれば収束。marker の sha と一致するので bak は作り直さない |
| `init_db` の途中 | DB は現行 `init_db` の挙動 (bak を作ってから rebuild) | 76 | 同上 |
| seed + 消費印の transaction の途中 | rollback 済み (DB は `init_db` 済み) | 76 | 同上 (冪等) |
| `setup.json` 更新後、`app_state.json` 更新前 | 記録は新、`initialized` は旧 | 76 | 同上 (記録は上書き) |
| `app_state.json` 更新後、marker を消す前 | 全部新 | 76 | 段 0 で marker の sha・settings・`initialized`・DB を照合し、揃っていれば「前回の確定は印を消す所だけが残っています。消しますか [Y/n]」で質問 0 回で収束し段 11 へ。揃っていなければ上の行と同じく段 0 から |
| marker を消した後 | 確定済み | 起動可 | 段 11 から (§3.1) |

marker が壊れて読めないとき: 内容 (sha) が無いので「前回の続き」とは判定せず、再実行は段 0 から。bak は新たに作る。サービスは止まったまま。

**引き渡し (段 13 手順 4〜6) の中断点**

| 落ちた点 | 実物の状態 | サービス | 再実行の再開地点 |
|---|---|---|---|
| unit 設置後、handoff の marker を置く前 | unit 在り。instance.lock は落ちた時点で解放 | 起動可 (確定 1 は済み。外から見る手段は未設定) | 段 0 から。段 13 では unit を「設置済み」と表示 |
| marker 後、instance.lock の解放前 | marker (`phase=handoff`) | 76 | 段 0 → 段 13 手順 3 から (marker の phase を見る) |
| instance.lock の解放後、enable の前・途中 | marker と、enable が持つ stage file / enroll 中の印 | 76 (enroll 中なら 75 が先) | 同上。enable の stage から同じ `afx ops enable` が続ける |
| enable の完了後、marker を消す前 | marker、stage は完了 | 76 | 手順 3 の質問は enable 側が 0 回で冪等に通り、marker を消す |
| marker を消した後、`start` の前 | 全部確定 | 起動可 | 再実行は不要。`systemctl --user start afx`。確認記録は `start: pending` として status が未完の手順に出す |
| `start` の後、起動後確認の途中 | 通常稼働 | 通常 | 確認記録に起動確認が無いまま。`afx ctl status` で確かめる |

### 4.4 settings の版と migration

- settings.yaml のトップレベルに `schema_version: <整数>` を持つ。example に 1 行 (コメントつき) 入れる。キーを持たない既存の settings.yaml は版 0 として扱う。
- 版ごとに明示的な migration を持つ (`setup/settings_migrate.py`、T1)。1 つの migration は次の操作の列だけで書く: 値変更は現在の行を更新、**新規の必須キーは、既知の migration だけが、example の同じ行 (コメントつき) を所定の block に挿入する**、rename と削除は migration 単位で明示する。バージョン非依存の汎用の差分マージは作らない。
- 位置が決まらない形 (block が無い、キーが複数行の形に書き換えられている、など未知の手編集) では**何も変えず**、手で直す箇所と足す行をそのまま表示して rc=1 とする。
- migration は段 10 の settings 置換に含める (別のファイル書き込みにしない)。段 9 の要約に、挿入・変更する行を出す。migration 後も、触らない既存の行は byte 一致のまま (IV-2)。
- 現行より新しい版の settings.yaml は変更せず、afx の更新を案内して rc=1。
- `--non-interactive` は migration を行わない。版が古ければ理由と `afx init` (対話) を案内して rc=1 (U-10: 非対話では migration しない)。サービス起動・`afx check` の必須キー欠落の文面には、版の不一致を添えて「`afx init` の再実行で移行します」と案内する。
- **運用責任 (settings schema を変える変更の完了条件)**: example の schema (必須キーの追加・rename・削除・既定値の意味の変更) を変える変更は、**同じ変更の中で**次の 5 点を揃える。揃わない変更は完了にしない: (1) `schema_version` を上げる、(2) 前版からの migration を足す、(3) 変更前の example を旧 example fixture として保存する、(4) 旧 fixture から現行への移行で触らない行が byte 一致であることの test を足す、(5) 変更の理由を migration の docstring と変更履歴に書く。T1 と T8 の完了条件にする。example を変える手元の作業が migration を伴わないと、次の example 更新で既存利用者の再実行が止まるため。
- 試験は、過去の各世代の example から作った settings.yaml を現行へ移行する形 (旧 example → 複数世代後) と、移行後に触らない行が byte 一致であること。

#### migration の履歴

| 版 | 日付 | 内容 | 理由 |
|---|---|---|---|
| v1 | 2026-10-12 | `schema_version` を導入し、v0 の unmarked settings を v1 へ移行する | 既存設定を再実行可能にし、以後の schema 変更を明示 migration で互換維持するため |

### 4.5 リセット (独立スクリプト `scripts/first-run-reset.sh`)

目的: 実機受入を `git clone` からやり直せるよう、ウィザードとその周辺が**当 instance のために**置いたものを取り除き、clean clone と同じ状態に戻す。

**手段は独立スクリプトを主にする** (内蔵の `afx init --reset` は後回しで、低チケット `afx-init-reset-builtin`。§10.3)。根拠:
1. リセットは、戻したい状態が壊れているときに使う道具である。実機受入のやり直しで戻したいのは、clone 直後ではなく「ウィザードが途中で止まった」「settings.yaml が新旧混在」「`uv sync` に失敗した」状態であり、`afx` のコマンドにすると venv・import・`Settings` の読み込みが前提になって、まさにその状態で動かない。
2. 前版は「消す対象の規則をシェルで二重に持つと食い違う」ことを内蔵にした根拠にしていた。食い違いの危険は残るが、規則は小さい固定表 (下記) で、食い違いは試験で検出できる (T15a の試験が、スクリプトの規則と ウィザード・afx-ops が実際に置く場所 (unit の置き場、鍵の置き場、feature の config の置き場) を同じ tmp 環境で照合する)。壊れうるものに乗せない価値のほうが大きい。
3. 入口が `afx init` に増えず、README の入口は `afx init` の 1 本のまま。

**スクリプトの前提**: bash、coreutils、`git`、`systemctl`、`loginctl`、`flock`、`python3` (標準ライブラリのみ、`python3 -I` で起動。Ubuntu 22 の 3.10 で動く範囲。`tomllib` は使わない)。**bash は orchestration (引数・確認・systemctl / loginctl の呼び出し・削除の実行) に限り**、所有の判定に要る次の 3 つだけを、小さな `python3 -I` の呼び出しに任せる: (a) root の `Path.resolve()` と `instance_id` の算出 (現行 `agentic_fx.ops.keys.instance_id` と同じ式 `sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]`)、(b) `data/state/setup.json` の linger 所有記録の JSON 読取り、(c) ops の `ops.toml` が当 root のものかの判定 (下記。TOML parser は自作しない)。パスのエスケープや JSON をシェルで再実装しない。`Settings`・`agentic_fx`・`.venv` を import しない。repo root は `$0` の位置から決め、cwd に依存しない。

**消す対象の規則** (スクリプトが自前で持つ。すべて「当 instance のもの」と判定できるものだけ):
- **repo 内** (固定の相対パス表): `config/settings.yaml`、`config/settings.yaml.bak-*`、`data/` (DB、`agentic.db.bak-*`、状態、確認記録、lock、ops の stage file)、`logs/`、`plugins/` 配下すべて (追跡ファイルは無く生成物だけ。同梱 example は `docs/examples/plugins/` にあるので消さない)、`policy/directives.md`。追跡されたファイル (`config/.gitkeep`、`config/settings.yaml.example`、`policy/.gitkeep` など) には触れない。`bak-*` のみ決まった接頭辞で列挙し、実行時に glob で広げない。
- **unit の置き場**: `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/`。`afx.service` と `afx.service.bak-*` は、それぞれの `WorkingDirectory=` が当 root の実パスと一致するものだけを当 instance の unit とする。一致しない・読めないものは「別 instance の unit を検出、触らない」と表示して除外する。`afx-ops@.service` と `afx.service.d/afx-ops.conf` (空になれば `afx.service.d/` も) は、`afx.service` が当 instance のものであるときだけ消す。
- **操作 API の鍵**: `$HOME/.config/agentic-fx/api/<instance_id>/` の**当 instance の分だけ**。base は `XDG_CONFIG_HOME` に関わらず常に `$HOME/.config` (現行 `keys.py` の配置どおり)。`instance_id` は上の (a) で求める。他の `api/<id>/` は「別 instance の鍵を検出、触らない」と表示して除外する。
- **ops の feature**: base は `${XDG_CONFIG_HOME:-$HOME/.config}/agentic-fx/ops/` (現行 `feature_config.py` の `_feature_base` と同じ。`XDG_CONFIG_HOME` は絶対パスのときだけ採る)。`<base>/<feature>/config/ops.toml` について、(c) の判定を行う: root から現行 writer が生成する canonical な 1 行 `instance_root = "<root の実パスを、`\` と `"` をエスケープして囲んだ値>"` (writer の `_toml_value` と同じ規則) を作り、`ops.toml` の中に**同じ行がちょうど 1 件**ある場合だけ「当 instance のもの (owned)」とする。異なる書式・手編集・行が 0 件または複数・`ops.toml` が無い feature は fail closed で除外し、「所有不明または別 instance の資材を検出、触らない」と表示する (拒否ではなく除外。他の対象の削除は続ける)。owned の feature の config・secret・state だけを消す。`~/.config/agentic-fx/` そのものは消さず、中が空になった `api/`・`ops/` だけ `rmdir` を試みる。
- **linger**: ウィザードが有効化した記録 (`data/state/setup.json` の `linger.state=owned_by_wizard`) があるときだけ、後述の確認を通して `loginctl disable-linger` を実行する。記録が無い・壊れている・`already_enabled`・`failed` のときは、`--include-linger` を付けても変更しない (「linger は手動で有効化されたものか、所有を確かめられないため触りません」と表示)。
- `.env` は利用者の秘密なので**既定では消さない**。`--include-env` のときだけ、対象一覧に載せたうえで消す。`.venv/`、`afx-ops/.venv/` は残す。

**動き**:
1. 前提検査: root に `config/settings.yaml.example` と `.git` が無ければ何もせず rc=2。root の実パスに改行など、canonical writer や unit で安全に扱えない文字 (制御文字を含む) があれば、計画の作成の前に拒否して rc=2。
2. **計画の作成** (何も消さない): 上の規則で、存在するものだけ種類別に全数を表示する。除外したもの (別 instance の資材など) は「触らない」として別枠に並べる。対象表示には次の一文を必ず添える: 「paper 取引履歴、注文、approval、改善 report、RAG、plugins を永久削除します (`data/`・`logs/`・`plugins/`)。復元できません」。`--dry-run` ならここで終わる。
3. **確認 (二重)**: 全数を見せたうえで `reset` の打ち込みを求める (Enter では通らない)。稼働中 (下記) のときは、これとは別に `stop` の打ち込みを求める。linger を解除する計画があるときは、さらに別に `linger` の打ち込みを求める。非端末では `--yes` が無ければ何もせず rc=2、稼働中なら `--yes` に加えて `--stop-running`、linger の解除には `--include-linger` が要る (`--yes` だけでは停止も linger の解除もしない)。
4. **稼働中の判定**: `afx.service` が当 instance のもので active、または当 instance の feature の `afx-ops@<feature>` が active、または `data/instance.lock` が保持されている。systemd が管理していない保持者 (手動起動) は止めず、「その端末で終了してください」で rc=2。続いて `data/ops/setup.lock` を非ブロッキングで取り、取れなければ別の `afx init` / `afx ops enable` が実行中なので rc=2。
5. **取り除く順序** (固定。途中で落ちても再実行で収束する冪等な順序。**所有の証拠 (`afx.service`、`setup.json`) は最後まで残す**。§5 の「リセット」列と同じ順):
   1. 当 instance の `afx-ops@<feature>` と `afx.service` を `stop` し、`disable` する。
   2. `~/.config/agentic-fx/` の当 instance の分 (所有を確かめた feature の config / secret、`api/<instance_id>/`) を消し、空になった親だけ `rmdir`。
   3. unit のファイルを消す: `afx-ops@.service`、drop-in、**最後に** `afx.service` と `afx.service.bak-*`。`afx-ops@.service` か drop-in が消せなかったときは `afx.service` を所有の証拠として残し (再実行で続きを消す)、rc≠0。`daemon-reload` と `reset-failed` は primary unit が当 instance 所有のときだけ実行し、`reset-failed` の非 0 (unit が not loaded) は許容する。
   4. repo 内の固定表 (`data/` と `.env` 以外)。repo 内の削除は `rm -rf` の前に `chmod -R u+rwX` で書込権限を戻す (plugin の版は `plugins/.versions/<name>/<hash>/` に dir 0o500 / file 0o400 の不変スナップショットとして置かれ、そのままでは消せない。symlink は chmod も rm も辿らない。v1.7)。
   5. linger: 上の規則で解除する計画のときだけ `loginctl disable-linger` を実行する (所有記録を読むのはここ。`data/` を消す直前。他の kind に失敗があれば解除しない)。
   6. `data/` (`setup.json` の所有記録は linger の処理後に消える。他の kind に失敗があれば残す)。
   7. `--include-env` のときだけ `.env`。
6. 終了時に「取り除いたもの」「触らなかったもの (別 instance の資材、`.env`、linger、`.venv/`)」を表示し、`git status --porcelain=v1 --ignored=matching` の結果を表示して、次は `afx init` と案内する。

安全:
- 消す対象は固定の相対パス表と上の所有判定から求める。repo root の下に解決できないもの、symlink で root の外へ出るものは辿らず、対象から外して表示する (`rm` の対象を実行時に glob で広げない)。
- unit 以外の systemd 状態と、`~/.config` の `agentic-fx/` 以外には触れない。他 instance の鍵・feature の config・secret は不変。
- このマシンのような稼働中の環境で打っても、手順 3 の確認を通さない限り何も消えない。試験は `tmp_path` の git clone、`HOME` と `XDG_CONFIG_HOME` を tmp に向けた環境、`PATH` 先頭に置いた fake の `systemctl` / `loginctl` だけで行い、実物の unit・鍵には触れない (conftest のガード対象)。
- **リセットの製品契約** (AC-36): 必須の取り除き対象は、wizard / daemon が所有する設定・データ・unit・鍵に限る (上の規則)。それ以外 (Python / uv の通常の生成物の `__pycache__/`、`.venv/` など) を取り除くことは契約に含めない。
- **試験の clean 判定** (AC-36a): 実 git clone の試験は、ウィザードの実行**前**の `git status --porcelain=v1 -z --ignored=matching` を baseline として保存し、reset 後の同じ出力と、NUL 区切りの repo 相対 path の集合で完全に比較する。reset が baseline に無い path として新たに残してよいのは固定の集合だけ (`.env` (`--include-env` を付けなかったとき)、`.pytest_cache/`、`.ruff_cache/`、`.venv/`、`afx-ops/.venv/`、`**/__pycache__/`、`tmp/`。後の 2 つは afx の通常稼働で増える無害な生成物で、reset の所有物ではない (2026-10-15 裁定))。ディレクトリ単位で許可した path (`.venv/` など) は、その内部を検査の対象外とする。それ以外の新しい path が出れば失敗する。`PYTHONDONTWRITEBYTECODE=1` で `__pycache__/` を作らせない設定に頼った結果だけを「実機の clean clone と同じ」とは呼ばない (baseline との差分が、通常実行の生成物を reset の所有物と取り違えず、未知の新規 artifact は検出する)。

---

## 5. 書き込み先の全数表

**方式の決定**: 「集めてから確定する」(段 10) と「段ごとに書く」(段 11〜13) の 2 層。確定 1 は複数のストレージにまたがるので「全部か無か」とは言わず、**回復可能な commit protocol** (§4.3) として定義する。
- 根拠 1: 設定 (settings.yaml) と DB 初期化と状態は互いに依存し、どれかだけ新しい状態は「起動したら何が起きるか」が読めなくなる。かといって YAML・SQLite・JSON を 1 つの transaction にはできない。だから (a) 集めて検査を通してから書き始め、(b) 書き始める前に durable marker を置いてサービスの起動を塞ぎ、(c) 順序を固定して各段を冪等にし、(d) 最後に marker を消す。途中で落ちても marker が残るので、新旧混在のままサービスが動くことは無く、再実行が同じ手順で収束させる。
- 根拠 2: 標準指標の bless は DB・`plugins/.versions`・履歴 git (`plugins/.history.git`) に書き、`init_db` 済みの DB と検証済みの設定を前提とする (inventory §5.2)。方針文・課題も DB とファイルに書く。これらは段 10 より前には実行できない。さらに bless は 1 本最長 300 秒の重い処理で、1 本ずつ人の確認の記録 (approval 行) を作るので、まとめて巻き戻す性質のものではない。だから 1 件ずつ確定させ、止めたらそこまでが残る形にする。
- 根拠 3: unit は「設定が確定し init が完了した後だけ」置く (inventory §7.3。完了前に置くと起動ガード rc=2 で再起動ループ)。だから最後。外から見る手段の有効化 (`afx ops enable`) は、その unit が在り afx が停止している間に行い、起動はさらにその後にする (P18、§6.3)。

「リセットで消すもの」列は `scripts/first-run-reset.sh` (§4.5) の対象の定義。「消す」は既定で消す (ただし `~/.config` 配下は当 instance のものと判定できたものだけ)、「残す」は消さない、「条件つき」は別の確認か指定が要る。

| 書き込み先 | いつ | 何を | 失敗したら | リセットで消すもの |
|---|---|---|---|---|
| `data/` + `data/instance.lock`、`data/ops/` + `data/ops/setup.lock` | 段 0 | 空の lock ファイル 2 つ (`instance_lock.py:33-34`、setup.lock は T9 と同じ fd 相対・`O_NOFOLLOW`) | 書けなければ rc=1 (権限の文面)。他は何もしない | 消す (`data/` ごと) |
| `data/state/setup_in_progress` | 段 10 の最初 (`phase=commit`)、段 13 の引き渡し (`phase=handoff`) | durable marker。在る間はサービスが 76 で起動しない | 置けなければ以降を書かず rc=1。消せなければ rc=1、再実行で消す | 消す (`data/` ごと) |
| `config/settings.yaml` | 段 10-2 | 一時ファイル → fsync → `os.replace` → 親 dir fsync。G1〜G3 を通った全文 (必要なら migration を含む) | 一時ファイルを消し、元のまま。rc=1。marker は残る | 消す |
| `config/settings.yaml.bak-<UTC>` | 段 10-2 (再実行のみ。前回の続きでは作り直さない) | 置き換え前の全文 | 書けなければ置き換えもしない | 消す |
| `logs/` | 段 10-3 | ディレクトリと技術ログ | 失敗は rc=1。settings は書けている旨を表示。marker は残る | 消す |
| `data/agentic.db` | 段 10-4 | スキーマ・移行 (`init_db`)、続けて 1 transaction でニュースソースと期の消費印 (`improve_waves` に `expected=0` の 1 行) | 移行失敗は現行の `init_db` の挙動 (`agentic.db.bak-*` を作ってから rebuild — `service.py:427-431`) に従う。transaction は失敗で rollback、marker は残り再実行で収束 | 消す (`data/` ごと。`agentic.db.bak-*` を含む) |
| `data/state/setup.json` | 段 10-5、以後の段と起動後確認で原子的置換 | 確認記録 (§7) | 段 10 では失敗として扱い marker を残す。以後の段では警告 (`status` は「確認記録なし」と表示) | 消す (`data/` ごと) |
| `data/state/app_state.json` | 段 10-6 | `initialized=true` (+ 初回のみ mode/autopilot) | 失敗は rc=1。「設定と DB は書けています。再実行してください」。marker は残る | 消す (`data/` ごと) |
| `logs/activity.log` | 段 10-7 | `init_completed` | 現行どおり | 消す (`logs/` ごと) |
| DB (approval・ledger・journal)、`plugins/.versions/<名>/`、`plugins/.history.git`、`plugins/<名>` symlink、`plugins/_human/<名>` (一時) | 段 11、1 本ずつ | 既存の bless 経路 | 1 本ごとに独立。gate 前の失敗は何も残らない、gate 後は journal が残り `approval retry` で収束 (サービス起動後) | DB は `data/` ごと。`plugins/` は配下すべて (追跡ファイルは無い。同梱 example は `docs/examples/plugins/`) |
| `policy/directives.md` | 段 12 | `- <text>` の追記 | 失敗は警告。何行書けたかを表示 | 消す (`policy/.gitkeep` は残す) |
| DB `improvement_backlog` | 段 12 | 1 件ずつ `source="user"` | 同上 | `data/` ごと |
| `~/.config/systemd/user/afx.service` (`$XDG_CONFIG_HOME` を尊重) | 段 13-2 | 生成した unit (direct exec)。既存があれば `afx.service.bak-<UTC>` に退避してから一時ファイル + rename | 書けなければ何も置かない。退避は置き換えの前 | 消す (`WorkingDirectory=` が当 root と一致するものだけ。`afx.service.bak-*` を含む。停止・`disable` のあと、ops 用ファイルの後に消す) |
| systemd の状態 (`daemon-reload` / `enable` / `start`) | 段 13-2 (`daemon-reload`)、段 13-6 (`enable` / `start`、個別確認) | `systemctl --user` を実行 | 失敗はコマンドと終了コード、`journalctl --user -u afx -n 30` を案内。unit ファイルは残す (「置いたが有効化していない」状態を表示) | 当 instance の unit に対して `stop` + `disable` + `daemon-reload` + `reset-failed` を実行 |
| linger (`loginctl`) | 段 13-6 (a)。`Linger=no` で、利用者が確認に `y` と答えたときだけウィザードが `loginctl enable-linger` を実行 | 確認記録 `setup.json` の `linger` (`owned_by_wizard` \| `already_enabled` \| `failed` \| 未確認) | 実行失敗は `failed` と reason を残し続行 (手で実行する案内) | 条件つき (`owned_by_wizard` の記録があり、別の確認 `linger` (非端末は `--include-linger`) を通したときだけ `disable-linger`。それ以外は触らない。記録は linger の処理後に消える) |
| `~/.config/agentic-fx/ops/<feature>/config/ops.toml` (0644)、`.../secret/` (dir 0700、Discord token・通知 webhook URL は file 0600) | 段 13-5、`afx ops enable <feature>` が書く。ウィザードは書かない | feature 専用の設定と秘密 (`ops.toml` は `instance_root` を持つ) | 危険・不正な既存設定は上書きせず固定 reason で止まる (afx-ops spec §7)。完了は妨げず、確認記録に残す | 条件つき (`ops.toml` の `instance_root` が当 root と一致する feature だけ消す。一致しない・判定できない feature は触らず表示) |
| `~/.config/agentic-fx/api/<instance_id>/` | 段 13-5 の enable が、鍵が未初期化のとき停止中に初期化。選ばなければ service が起動時に補完 | 操作 API の principal 鍵 (`<instance_id>` は root の実パスの SHA-256 先頭 16 桁で、instance ごとに分かれる) | enable の失敗は同上 | 消す (当 instance の `<instance_id>` だけ。他の `api/<id>/` は触らず表示) |
| `data/ops/<feature>.stage.json`、`data/ops/enroll_in_progress` (enroll 中だけ) | 段 13-5 の enable | 有効化の durable な状態 | 同じ `afx ops enable <feature>` の再実行で再開 | 消す (`data/` ごと。先にスクリプトが unit を停止・無効化する) |
| `~/.config/systemd/user/afx-ops@.service`、`afx.service.d/afx-ops.conf` (`Wants=afx-ops@<feature>.service`) | 段 13-5 の enable | ops 用 unit の雛形と afx 側の引き込み | 同上。「あとで」では置かない | 消す (`afx.service` が当 instance のものであるときだけ。`afx.service.d/` は空になれば) |
| `.env` | 書かない | — | — | 条件つき (`--include-env` のときだけ消す。既定は残す) |
| unit の環境に秘密 | 書かない | — | — | — |
| Discord token・通知 webhook URL | ウィザード本体は書かない。書くのは `afx ops enable` の secret file だけ | — | — | 上の `~/.config/agentic-fx/` ごと |
| `config/settings.yaml.example`、`config/.gitkeep`、`policy/.gitkeep`、`docs/examples/plugins/`、`.venv/`、`afx-ops/.venv/` | — | — | — | 残す (追跡ファイルと環境) |

---

## 6. unit の設置と外から見る手段

### 6.1 生成する内容 (direct exec)

本番で稼働中の unit (`deploy/afx.service.direct`) の形を写す。固定の行と、実行環境から埋める値を分ける。

```
[Unit]
Description=agentic-fx paper trader (daemon)
After=network-online.target{bridge_after}
Wants=network-online.target{bridge_wants}

[Service]
Type=exec
NotifyAccess=none
Delegate=pids memory
WorkingDirectory={repo_root}
ExecStart={python} main.py --daemon
SendSIGKILL=yes
FinalKillSignal=SIGKILL
TimeoutStopFailureMode=terminate
TimeoutStopSec=630
KillMode=control-group
Restart=on-failure
RestartSec=30
RestartPreventExitStatus=75 76
StandardOutput=journal
StandardError=journal
Environment=LANG={lang}
Environment=PYTHONUNBUFFERED=1
Environment=PATH={path}

[Install]
WantedBy=default.target
```

固定の行の意味:
- `Type=exec` と `ExecStart={python} main.py --daemon`: 対話シェルを持たない daemon として直接 exec する。`ExecStop` は書かない。停止は SIGTERM で graceful に入る (`service.py` の `stop_event`)。`SendSIGKILL` / `FinalKillSignal` / `TimeoutStopFailureMode=terminate` / `TimeoutStopSec=630` / `KillMode=control-group` は、実行中 mission の待ちを含む停止の上限と、残った子 process の確実な回収のため。
- `Delegate=pids memory`: 改善 CLI 隔離の admission が mission ごとの cgroup を作る条件 (`docs/superpowers/specs/2026-10-07-improve-cli-seccomp-design.md` §6.1)。欠けると admission が `cgroup_topology_unsupported` などで通らず、CLI backend の改善は止まる。
- `RestartPreventExitStatus=75 76`: 75 は `afx ops enable` の enroll 中に afx が専用終了コード 75 で即終了する契約 (afx-ops spec §6.1.2)、76 は `afx init` の確定途中・引き渡しの間にサービスが `setup_in_progress` で即終了する契約 (§4.3) のため。欠けると再起動を繰り返す。`afx ops status` は 75 の欠落を `warn=afx_restart_prevent_missing` で示す (75 を含むかだけを見るので、76 を足しても判定は変わらない。`stages.py:388`)。76 を含まない既存の unit は、`afx init` の再実行で作り直す (U-9)。
- `Restart=on-failure` / `RestartSec=30`: 起動前検査の失敗で再起動ループにならないよう §6.2 の検査を先に通す。

埋める値:
- unit 名は `afx.service` 固定。`afx ops enable` がこの名前で停止・照会し、`afx.service.d/` に drop-in を置く (P18)。
- `{repo_root}`: 段 0 で確かめた cwd の実パス (`entry.py` が cwd を root にするので必須)。
- `{python}`: `{repo_root}/.venv/bin/python`。存在を確かめ、無ければ「`uv sync` を先に実行してください」と表示して unit を置かない。
- `{repo_root}` と `{python}` が、systemd の引用なしでは書けない文字 (空白、引用符、`%`、`$`、`\`、制御文字) を含むなら置かない。理由を表示する。afx-ops の `ops_exec` 検査 (`ops_setup/units.py`) と同じ規則。
- `{path}`: `shutil.which` で claude / codex / opencode (選ばれた backend のみ) を解決し、その親ディレクトリ + `/usr/local/bin:/usr/bin:/bin`。**nvm のバージョン入りのパスを固定値として持たない** (P11)。解決したパスが nvm 配下なら「node を更新したら `afx init` を再実行して unit を作り直してください」と表示し、未完ではなく注意として記録に残す。
- `{bridge_after}` / `{bridge_wants}`: `~/.config/systemd/user/` に `mt5-bridge.service` があれば足す。無ければ足さず、「bridge を systemd で動かしているなら、その unit 名を After= に足すと起動順がそろいます」と表示。bridge 不通は afx 側で degraded として扱うので必須ではない (`docs/operations/afx-service-2026-09-26.md` 構成表)。
- `{lang}`: ウィザード実行時の `LANG` (無ければ `C.UTF-8`)。未確認: 表示の文字化けを防ぐのに十分か。
- 秘密は入れない (`Environment=`・`EnvironmentFile=` とも)。CLI backend の検査⑤で起動拒否になる (inventory §2.5)。

### 6.2 置く前の検査 (C-7 の再起動ループを防ぐ)

1. `afx check` (新設、task T7a) を **unit と同じ環境** で実行する: `env -i HOME=$HOME PATH={path} LANG={lang} PYTHONUNBUFFERED=1 {python} main.py check` を cwd=`{repo_root}` で。`check` は settings 読み込み・起動前検査のうち設定と実行環境で決まるもの (cache retention、mission ceiling、CLI backend の bin と認証ファイルと検査⑤) を実行し、サービスは起動しない。lock・DB 書き込みはしない (読み取りのみ。実装で pin)。
2. `check` の結果は **fatal** と **warning** に分ける。分類は 1 か所 (T7a で `afx check` と service の起動前検査が共有する関数) に持つ:
   - **fatal** (起動不能。unit を置かない): settings の構文・型・必須キー欠落、mission ceiling 違反、cache retention の不整合、CLI backend の実行ファイル形式の誤り (例: codex が ELF でない)、検査⑤ (秘密 env の存在) など、サービスが起動できない条件。「unit の環境では … (PATH に {dir} がありません)」のように理由を出す。
   - **warning** (LLM 未検証。unit を置く): `deferred_by_user`、未確認モデル (`/models` が取れない・一覧に無い)、CLI の未導入 (`bin_not_found`)・未認証 (`credentials_missing`)、`user_declined_probe` など、LLM が使えるかが確かめられていないだけの条件。unit は設置し、起動後は data state / mission が degraded になり、未完の手順に項目が出続ける。warning は取引判断 mission が実行時に fail closed で止まることで守られる。
   - 結果は `{fatal: [...], warning: [...]}` と固定 code で返し、終了コードは fatal がある場合のみ非 0。warning だけなら 0 で、warning の一覧を表示する。
3. `systemctl --user` が使えない (コンテナ・WSL の一部) 場合: unit を置かず、手動起動 (`uv run afx`) の案内。
4. 既存の `afx.service`: 内容の差分を表示し、「置き換えますか [y/N]」。稼働中なら段 0 の lock で既に止まっている。既存が screen 時代の形 (`ExecStart` が `afx-start.sh`) なら、「対話シェル (`screen -r afx`) はなくなり、操作は `afx ctl` になります」と添える。
5. `Delegate=pids memory` が user manager で実際に効くか (cgroup の委譲) は、置く前には確かめられない。起動後の admission の結果 (§6.4) で見る。

### 6.3 外から見る / 操作する手段 (`afx ops enable` を呼ぶ)

- 呼び出しの前提: unit が `afx.service` として在ること (段 13 の手順 2 の後)、引き渡し (手順 4) が済んでいて instance.lock をウィザードが持っていないこと、marker が `phase=handoff` で在ること、setup.lock をウィザードが保持していること、afx が停止していること。
- 呼ぶ前に afx-ops の起動コマンドが解決できるかを `resolve_ops_exec` と同じ規則で調べ (P17)、無ければ「afx-ops が見つかりません。`afx-ops/` を PATH に置くか `afx_ops` を import できる環境にしてください」と表示して、その feature を「あとで」に倒す。
- feature ごとに `afx ops enable <feature>` 相当を呼ぶ。実体は同一 process での `interactive_enable(root, feature, lease=<保持済みの setup lease>) -> EnableResult` の呼び出しで、子 process にしない。
- **ウィザードの IO と ops の IO は別の型にする**。ウィザードの IO (`WizardIO`) は `isatty` / `ask(prompt)` / `say(text)` だけを持ち、秘密を扱う口を持たない。ops の IO (T9 の `IO`) は T9 が所有し、`ask_secret` は端末へ直結する (非エコー)。ウィザードは ops の IO を作らず、渡さず、受け取らない (production では `interactive_enable` が自分で端末の IO を作る)。したがって token・webhook URL の値はウィザードの変数にも出力にも現れない。テスト用の secret 入力の差し込み口は T9 のテストだけに置き、ウィザードのテストは `interactive_enable` そのものを fake に差し替える (呼び出し記録と `EnableResult` の返却だけを確かめる)。
- **T9 側の変更 (実装 task T11。T9 は main 投入済みなので、この束で足す)**: (1) `interactive_enable` に `lease` 引数を足す。lease が渡されたときは自分で setup.lock を取らず、lease が同じ root のもので保持中であることだけを確かめる (違う root・解放済みなら固定 reason `setup_lease_invalid` で止まる)。既存の対話 CLI の経路 (`afx ops enable <feature>`) は lease 無しで、これまでどおり自分で取る。(2) 戻り値を `int` から `EnableResult(rc, stage, reason_code)` にする。reason は stdout / stderr の文面を解析せず、戻り値で得る (stage は enable の最終 stage、reason_code は固定語彙)。CLI のラッパーは `EnableResult.rc` を終了コードにする。(3) setup.lock の取得を、ウィザードが同じ手順で取れる公開関数 (`acquire_setup_lease(root)`) に切り出す。
- 質問する内容は enable 側が決め、既存設定が揃っていれば質問 0 回で冪等に通る。非端末は固定 reason で止まる。
  - bot: Discord token (非エコー)、guild、channel、承認者 allowlist。
  - web: loopback の port。認証は無い (127.0.0.1 限定、読み取り専用)。
  - notify: HTTPS webhook URL (secret に置く)。Discord 向けの主経路は bot で、notify は補助。
- enable は afx を起こさない (afx_before_stop が inactive のため)。ops 用 unit は afx の次の起動で `Wants=` により一緒に起きる。したがって enable の完了時点では ops の ready は観測されておらず、最初の ready 観測は §6.4 の起動後になる。
- 失敗は完了を妨げない (IV-11)。`EnableResult` の `rc` / `stage` / `reason_code` を確認記録の `ops.<feature>` に残し、「`afx ops enable <feature>` を再実行してください」と案内する。途中で止まった enable は同じコマンドの再実行で続く (afx-ops spec §6.1)。
- 「あとで」(既定) では何もしない。ops 用 unit も `Wants=` も、引き渡し (marker) も行わない。

### 6.4 有効化・起動、起動後確認、linger

- `enable` → 起動、をそれぞれ確認する。起動の確認文: 「今起動すると、価格の取り込みと (待つを選ばなかった場合) 改善 mission が始まります。起動しますか [y/N]」。起動の前に instance.lock と handoff の marker は片付いている (§4.2、IV-9)。
- **warning だけで置いた unit の起動**: `afx check` が warning のみのとき (§6.2)、起動後は degraded と表示する。ウィザードは「LLM が未検証です。未完の手順: …」と、手順書の節名つきの案内を終了表示に出す。段 1 の `active` と段 2 の認証つき status は通常どおり確かめる。
- **起動後の確認は 3 段階**で、それぞれ独立に記録する。前の段が失敗しても、記録できる段は記録し、ウィザードは完了する。
  1. **サービス**: `systemctl --user` で `afx.service` が `active` か (数秒見る)。起動の直前に `InvocationID` と journal の cursor を取り、起動後に新しい `InvocationID` を取る (下の admission 行の帰属に使う)。
  2. **操作 API**: 鍵認証つきの UDS で `afx ctl status` と同じ呼び出しが、有界時間 (例 30 秒。2 秒間隔の再試行) 内に成功するか。サービスが `active` でも、`start_ops_api()` は鍵・回復・bind の失敗で daemon を止めず警告だけで返す (`service.py` の `start_ops_api`) ので、`active` は API の ready を意味しない。結果は `api.result: ok | failed | disabled` と固定の `reason_code` (`socket_missing` / `auth_failed` / `timeout` / `not_ready` …) で、サービスの成否とは別に記録し、`failed` は未完の手順にする。**操作 API を無効にした場合**: `afx ctl status` は使えず、未完の手順を `afx ctl status` に出すことはできない。ウィザードの終了表示と起動画面 (splash、daemon では journal) だけが案内の窓口である旨を、段 8 と段 14 で明示する。
  3. **ops feature**: 選んだ feature ごとに `afx-ops@<feature>` が active で、`afx ops status <feature>` が ready か。ops は `Wants=` で引かれ、afx の操作 API が ready になるまで有界に待つので、数十秒かかることがある (afx-ops spec §6.2)。段 2 が失敗なら ops の ready は期待できないので、その旨を添える。
- **journal の観測契約** (今回の起動分だけを読むための具体手順。T7b の着手前に固定する):
  1. 起動の直前に `systemctl --user show afx.service -p InvocationID --value` で前回の InvocationID (起動前は空のことがある) を取り、続けて `journalctl --user -u afx.service -n 0 --show-cursor` の `-- cursor: …` を取る (fallback 用)。
  2. `start` の後、同じ `show` を有界時間内に繰り返し、**前回と異なる空でない InvocationID** が出たものを今回の起動とする (同じ値のまま、または空のままなら今回の起動と確認できない)。
  3. journal は `journalctl --user -u afx.service -o json --no-pager` で読み、各行の JSON から今回の InvocationID に一致する行だけを採る。照合する field は `_SYSTEMD_INVOCATION_ID` を第一に、無ければ `USER_INVOCATION_ID` (user journal ではこちらの可能性がある) を使う。どちらの field も、`-u afx.service` の行に 1 件も出ないときだけ「field が使えない」と判定する。
  4. fallback: 「field が使えない」と判定した場合、または InvocationID が取れなかった場合に限り、手順 1 の cursor より後 (`--after-cursor`) だけを読む。このとき記録の `reason_code` には絞り込みの方式 (`invocation_id` / `cursor`) を添える。cursor も取れなければ `journal_unavailable`。
  5. 採った行のうち `improve isolation:` で始まり `backend=` が現在の改善 backend と一致するものの、最後の妥当な行を使う (下の規則)。
  6. この契約の実機での確認 (field の実在、Ubuntu 22 と systemd 有効の WSL の差) は T14 の観測表に載せる。確認の結果、field が違っていた場合は、契約の手順 3 の field 名だけを直す。
- **改善 backend の隔離適合 (admission 行)**: 改善 backend が CLI のとき、journal の起動行 `improve isolation: enabled; backend=… admitted=yes|no [reason=…]` を読む。**今回の起動分だけ**を読む: `InvocationID` で絞る。観測契約は次の項で固定する (実装 task T7b の着手前提。実機での確認は T14)。行の `backend=` は現在の settings の改善 backend と一致するものだけを採り、該当が複数なら今回の起動分の最後の妥当な行を使う。前回の起動の `admitted=yes` を今回の結果と取り違えない。結果は `isolation.admitted: yes | no | unknown` と `reason_code`:
  - 行が読めて `admitted=no reason=…`: reason を記録し、未完の手順に出す。
  - journal を読めない (`journalctl` が無い・権限): `journal_unavailable`。
  - 行の形式が不正: `journal_format_invalid`。
  - 有界時間内に行が出ない: `admission_line_timeout` (`unknown`。待ち続けず、確認コマンド `journalctl --user -u afx | grep 'improve isolation'` を案内して終える)。canary と自己試験は起動後に別 thread で走るので、行が出るまで数分かかることがある。
  - backend が local のときの行 (`is not a CLI backend`) は admission の対象外。`isolation` は必須キーのままで、`{admitted: unknown, reason_code: not_cli_backend}` を記録する。
- linger: 段 13 の手順 6 (a) で扱う (§3 段 13)。`Linger=no` のときだけ、確認つきでウィザードが `loginctl enable-linger $USER` を実行し、成功したら所有記録を確認記録に残す。利用者が断った、または実行に失敗した (環境によっては管理者権限が要る) ときは、「ログアウトすると afx も止まります。止めたくなければ `loginctl enable-linger $USER` を実行してください」と案内して続行し、未完の手順 (`linger_off`、情報) に出す。すでに `yes` のときは何も変えず所有も主張しない。仮運用文書は「前提: Linger=yes」(`docs/operations/afx-service-2026-09-26.md` 前提節)。

---

## 7. `status` と起動画面が出し続ける「未完の手順」

### 7.1 確認記録 `data/state/setup.json`

- 内容: `version`、`completed_at` (段 10)、`mode` (`interactive` | `non-interactive`)、`settings_schema_version`、`price` / `llm_trade` / `llm_improve` の各 `{result: ok|failed|unverified|planned, at, reason_code}`、`bridge_plan` (`connected` | `planned` | `unused`)、`history` (`{source: mt5|none|…}`)、`unit` (`installed` | `placed_not_reloaded` (unit は置いたが `daemon-reload` に失敗し有効化されていない) | `not_installed`、`launch: direct`)、`start` (`{result: started|pending|skipped, at}`)、`api` (`{enabled: true|false, result: ok|failed|disabled|unchecked, at, reason_code}`。`result` は段 13 の起動後確認 (b) の結果で、サービスの成否とは別)、`ops` (feature ごとに `{result: enabled|failed|later, stage, at, reason_code, ready: ok|failed|unchecked}`。`failed` の `stage` と `reason_code` は `EnableResult` の値、`ready` は起動後確認 (c))、`isolation` (必須。`{admitted: yes|no|unknown, at, reason_code}`。起動後確認の admission 行から。改善 backend が CLI でないときは `{admitted: unknown, reason_code: not_cli_backend}`)、`linger` (`{state: owned_by_wizard|already_enabled|declined|failed|not_checked, at, reason_code}`。`not_checked` は確定 1 の時点の値 (段 13 手順 6 で確認するまでの未実施)で、未完の手順 `linger_off` は `declined` / `failed` のときだけ出す。`owned_by_wizard` は、ウィザードが確認つきで `loginctl enable-linger` を実行し `Linger=yes` を再読込で確かめた場合だけ。リセットが解除してよい唯一の根拠で、手動で有効化された linger は `already_enabled` として所有を主張しない)。確認記録への追記は、サービスが読み取りだけで使えるよう、一時ファイル + fsync + rename + 親 dir の fsync の原子的な置換で行う。
- 自由文・URL・例外文面は保存しない。失敗理由は固定の分類 (`connect_error` / `auth_required` / `server_time_unknown` / `terminal_disconnected` / `model_not_found` / `bin_not_found` / `credentials_missing` / …)。
- 読めない・壊れている場合は `record_state: corrupt` (無ければ `missing`) として「確認記録が読めません。`afx init` を再実行してください」の 1 行にして、他の表示は続ける (fail-soft)。起動を止めない (記録は資金保護に関わらない)。

### 7.2 一覧 (その場の事実から算出するものと記録から読むもの)

| 項目 | 出す条件 | 文面の例 | 事実の出どころ |
|---|---|---|---|
| ウィザード未実施 | `initialized=true` で記録が無い (既存利用者・作者の環境) | 「初期設定の確認記録がありません。`afx init` を再実行すると接続を確認して記録します」 | 記録の有無 |
| LLM 未確認 | `llm_*` が `ok` 以外 | 「取引判断の LLM (local / <model>) は未確認です (2026-10-02 15:10、モデル一覧を取得できませんでした)」 | 記録 |
| opencode 未検証 | improve が opencode で `llama_swap_verified: false` | 「`afx improve verify-backend --backend opencode` の合格後に improve.llama_swap_verified を true にしてください」 | 設定 |
| bridge 未接続 | `bridge_plan=planned`、または data state が `degraded` | 「MT5 bridge はまだつながっていません。残りの手順: …」(段 3 の案内 1〜7 のうち未了の推定。推定できないものは全部) | 記録 + 既存の data 行 (`commands.py:369`) |
| `.env` の不足キー | §3 段 8 の規則で必要なキーが無い | 「.env に DISCORD_WEBHOOK_URL がありません (Discord 通知は無効のままです)」 | 環境変数の有無のみ。値は見ない |
| 標準指標未導入 | 9 本のうち未配備がある | 「標準指標 9 本中 3 本が未導入です (`afx init` の再実行か `afx plugin bless <名> --from _human`)」 | 配備済み plugin の一覧 |
| 戦略なし | 配備済みの strategy plugin が 0 | 「戦略がまだありません。取引判断は LLM の判断だけで行われます。改善ループが作った戦略は `afx ctl approval list` で確認・採用します」 | 配備済み plugin |
| 履歴なし | `eval_source` が `none`、または履歴がペアごとに 0 件 | `none`: 「履歴源が設定されていません。MT5 bridge をつなげて `backtest.eval_source` を `mt5` にするか、別の対応履歴源を設定してください」/ 0 件: 「戦略の採用審査に使う履歴がありません (USDJPY)。<履歴源ごとの手順>」 | 設定 + DB の件数 (`none` の固定理由は strategy gate と同じ `history_source_unconfigured`) |
| 改善待ちの approval が止まっている | gate 後で止まった journal がある | 「承認 #12 が途中で止まっています。`afx ctl --as approver approval retry 12`」 | 既存の journal |
| Landlock 無し | `is_available()` が偽 | 「この環境では改善ループと plugin の採用ができません」 | その場の検査 |
| 週明けの停止 (情報) | 源が yfinance | 「yfinance は週明けにしばらく止まります (既知)」 | 設定 |
| 外から見る手段の有効化が未了 | `ops.<feature>=failed` | 「bot の有効化が途中です (reason: <固定 reason>)。`afx ops enable bot` を再実行してください」 | 記録 (reason は固定語彙のみ。途中の stage の詳細は `afx ops status <feature>` に任せる) |
| 改善 CLI の隔離が通っていない | `isolation.admitted=no` | 「改善の CLI backend は隔離の適合検査を通っていないため実行されません (reason: <reason>)。起動行は `journalctl --user -u afx \| grep 'improve isolation'`」 | 記録 |
| 操作 API が無効 | unit 設置済みで `api.enabled=false` | 「操作 API が無効です。常駐中の承認・方針文の追加ができません。settings.yaml の api.enabled を true にして再起動してください」(**`afx ctl status` には出せない**。splash とウィザードの終了表示だけ) | 設定 + 記録 |
| 操作 API の起動確認が失敗 | `api.result=failed` | 「サービスは起動しましたが、操作 API に認証つきで接続できませんでした (reason: <固定 reason>)。`journalctl --user -u afx -n 50` を見てください」(API が応答しないので splash とウィザードだけで出る。API が後から ready になれば `afx ctl status` にも出る) | 記録 |
| linger が無効 | unit 設置済みで `linger.state` が `declined` または `failed` | 「ログアウトすると afx も止まります。`loginctl enable-linger $USER` を実行してください」(情報) | 記録 |
| 起動が未了 | unit 設置済みで `start=pending` | 「サービスはまだ起動していません。`systemctl --user start afx`」 | 記録 + その場の `is-active` |
| ops の ready が未確認 | `ops.<feature>.ready=failed` | 「bot は有効化済みですが ready を確認できていません。`afx ops status bot`」 | 記録 |

- 表示場所: 起動画面 (splash、daemon では journal に出る) に件数と先頭 3 件、`afx ctl status` の出力の末尾に「未完の手順 (N 件)」節。常駐では対話シェルの `status` が無く、`afx ctl status` は操作 API の `status` (`ops/service.py` の status provider) を表示するので、節の算出は daemon 側の読み取りに置く。**操作 API が無効なとき `afx ctl` は使えない**ので、無効という事実と API 起動確認の失敗は splash とウィザードの表示にだけ出る。0 件なら何も出さない。status 応答の項目は §7.3 の固定 schema。
- 算出は軽い読み取りだけ (ネットワークに出ない)。外部接続の現在の状態は data state と mission の結果が既に示しているので、`status` が自分で接続しに行くことはしない。

### 7.3 操作 API の status 応答に足す項目 (wire contract)

status の応答 (`GET /v1/status`、権限 `status.read`) に次の節を足す。日本語の文面やコマンドの表示は応答に載せず、固定の code と params だけを載せて、client (`afx ctl`、bot、web) と splash が生成する。

```
setup: {
  record_state: "ok" | "missing" | "corrupt",
  pending_steps: [ { code, severity, observed_at, params } ]   # 最大 32 件
}
```

- `severity`: `info` | `warn` | `action`。`observed_at`: UTC の ISO 8601 (記録の時刻、またはその場の事実を読んだ時刻)。
- `code` は固定の語彙 (列挙は schema で持つ): `setup_record_missing`、`llm_unverified`、`opencode_unverified`、`bridge_not_connected`、`env_key_missing`、`indicators_missing`、`no_strategy`、`history_missing`、`history_source_unconfigured`、`approval_stalled`、`landlock_unavailable`、`yfinance_weekend_gap`、`ops_incomplete`、`ops_not_ready`、`isolation_not_admitted`、`api_start_failed`、`api_disabled` (splash とウィザードの終了表示だけ。`afx ctl status` の列挙からは除く)、`start_pending`、`linger_off`。`params` は code ごとに固定のキーを持ち、値は列挙語・整数・`[a-z0-9_.-]{1,64}` の識別子・UTC の時刻だけ (例: `env_key_missing` は `{key}`、`ops_incomplete` は `{feature, stage, reason_code}`、`approval_stalled` は `{approval_id}`)。
- **載せないもの**: URL、例外文、パス、モデルの自由名、秘密値、reason の自由文。理由は固定の `reason_code` で表す (`safe_error_text` の対象になる文字列は応答に入れない)。status.read は operator / approver / ops-bot / ops-web の全 principal が持つので、秘密や内部の詳細が漏れない形に限る。
- **前方互換**: client は未知の `code` を、「未知の手順 (code=…)。afx を更新してください」の 1 行で表示し、`params` は表示せず、エラーにしない。未知の `severity` は `info` として扱う。未知のキーは無視する。
- `record_state=missing` のときは `setup_record_missing` 1 件だけを出す (他の項目はその場の事実から算出できるものだけ)。`corrupt` は「確認記録が読めません」の 1 件に加え、その場の事実から算出できる項目を続ける。
- 応答の schema・受入条件・権限表を改訂する対象は、操作 API の仕様 (`docs/superpowers/specs/2026-10-04-ops-api-design.md` §4.2 行 1 の status、§10 不変条件、§11 受入条件) で、権限の種類は増やさない (status.read のまま)。この改訂は実装 task T12 の一部で、この束の別 task として列挙する。splash に出す文面は T3 の renderer が同じ code から生成する。


---

## 8. 不変条件・受入条件・テスト方針

### 8.1 不変条件

| ID | 不変条件 |
|---|---|
| IV-1 | 段 10 より前に、`data/` と `data/ops/` のディレクトリ、`data/instance.lock`、`data/ops/setup.lock` 以外を書かない。 |
| IV-2 | settings.yaml の書き換えは、回答で値が変わる行と、既知の migration が定める行 (§4.4) だけを変える。他の行はバイト一致 (migration の後も)。書く前に G1 (重複キー無し)・G2 (意味的照合)・G3 (型検証) を全部通す。位置が決まらない未知の形は変更せず手修正を案内する。 |
| IV-3 | ウィザード本体は秘密を受け付けず、書かず、表示しない。`.env` を作らない・変えない。unit の環境に秘密を入れない。`secret_env_allowlist` に足さない。認証ファイルを開かない。`afx ops enable` の対話層が非エコーで受ける token・webhook URL は、その端末入力から feature の secret file (0600) に直接書かれ、ウィザードの出力・確認記録・ログ・子 process の argv / env / stdin に現れない。 |
| IV-4 | 価格源は選んだ 1 つだけを `enabled: true` にし、他は `false` にする (無言の切替を設定で作らない)。 |
| IV-5 | plugin の採用は、一覧表示の後の明示的な `y` 入力でだけ行い、既存の bless 経路 (gate・journal・ledger) を通す。既定は採用しない。`plugins/` に直接置かない。 |
| IV-6 | 資金保護の項目 (`risk.*`) と kill switch をウィザードは尋ねず、変えない。kill switch の latch に触れない。 |
| IV-7 | 稼働中のサービスがあるとき (instance lock が取れない) は何も書かずに止まる。`--non-interactive` も同じ。 |
| IV-8 | 再実行は、答えなかった項目・状態 (`mode`・`autopilot`・latch) を変えない。注意が要る項目の変更は帰結の表示と `yes` の打ち込みを経る。 |
| IV-9 | systemd でサービスを起動する操作は、instance.lock と handoff の marker が片付いた後の最後の操作であり、起動後にウィザードは設定・DB・`app_state.json` を書かない。許すのは確認記録 `setup.json` の原子的な置換だけ。引き渡しの間 (marker の設置から除去まで) も同じ。 |
| IV-18 | 操作 API を有効にする回答は `api.enabled: true` の 1 行だけを変える。ウィザードは鍵を作らない (外から見る手段を選んだときの enable と、service の起動時補完に任せる)。 |
| IV-11 | 外部接続の失敗は完了を妨げない (B-6 b)。失敗・未確認は確認記録に残り、`status` が出し続ける。 |
| IV-12 | 非 tty で `--non-interactive` が無ければ、何も書かずに理由を表示して rc=2。黙って非対話に切り替えない。 |
| IV-13 | 期の消費印は「待つ」を選んだときだけ、supervisor と同じ関数で求めた直近の期に 1 行だけ置く。supervisor のコードは変えない。 |
| IV-14 | Dukascopy への取得をウィザードは実行せず、既定として勧めない。MT5 を使わない分岐では `backtest.eval_source: none` を必ず書く。`none` から履歴源を推測しない。 |
| IV-15 | ウィザードが表示する外部由来の文字列 (例外文面・モデル名一覧) は `safe_error_text` と 1 行化を通す。 |
| IV-16 | `afx ops enable` 相当は、`afx.service` が設置済みで、ウィザードが instance.lock を解放し handoff の marker を置いており、afx が停止している間にだけ、保持済みの setup lease を渡して呼ぶ。ウィザードは enable の質問・疎通確認・状態機械を再実行せず、秘密を受け取る口を持たず、`EnableResult` だけを読む。 |
| IV-17 | 外から見る手段の既定は「あとで」。「あとで」と全 off では ops 用 unit と `afx.service.d/afx-ops.conf` を置かない。ops の有効化の失敗はウィザードの完了を妨げない。 |
| IV-18 | 操作 API を有効にする回答は `api.enabled: true` の 1 行だけを変える。ウィザードは鍵を作らない (外から見る手段を選んだときの enable と、service の起動時補完に任せる)。 |
| IV-19 | 確定 1 は最初に durable marker (`data/state/setup_in_progress`) を置き、最後に消す。marker が在る間、サービスは終了コード 76 で起動しない。判定は `run_service` の root 解決の直後、`ensure_initialized`・`.env`・settings 読み込みより前の 1 箇所で、marker の存在だけで行う (内容を読まない。`ENOENT` 以外の失敗は「在る」で fail closed)。両方在るときは 75 が先。確定 1 のどの中断点でも、同じ `afx init` の再実行が収束させる (§4.3)。 |
| IV-20 | lock は `data/ops/setup.lock → data/instance.lock` の順にだけ取る。`afx init` は setup.lock をウィザード終了まで保持し、引き渡しは marker → instance.lock 解放 → enable → marker 除去 → (起動直前の最終確認) の順でだけ行う。 |
| IV-21 | 起動後確認は、サービス active・操作 API 認証つき status・ops ready の 3 段階を別々に記録し、サービスの成功を API や ops の成功とみなさない。 |
| IV-22 | journal の admission 行は今回の起動分 (InvocationID または起動前の cursor 以降) で、現在の改善 backend と一致するものだけを採る。journal 不可・timeout・形式不正は別の reason。 |
| IV-23 | status 応答の setup 節は固定 schema の code と params だけを載せ、URL・例外文・パス・秘密値・自由文を載せない。未知の code は client が前方互換で扱う。 |
| IV-24 | ウィザードの IO は秘密を扱う口を持たない。ops の IO は端末へ直結し、ウィザードに値を返さない。 |
| IV-25 | `scripts/first-run-reset.sh` は、消す対象の全数を表示して `reset` の打ち込みを得るまで何も消さない。非端末は `--yes` が無ければ何もしない。稼働中 (instance.lock 保持者、active な unit) があれば、別の停止確認 (`stop`。非端末は `--stop-running`) を得るまで停止も削除もしない。手動起動の保持者は止めない。venv・`Settings`・`agentic_fx` の import に依存せず、settings.yaml が壊れていても動く。 |
| IV-26 | リセットが消す・止める・解除するのは当 instance のものと判定できたものだけ: `~/.config/agentic-fx/api/<instance_id>/` は当 instance の分のみ、ops の feature は `ops.toml` の `instance_root` が当 root と一致するもののみ、`afx.service` 系の unit は `WorkingDirectory=` が当 root と一致するもののみ。別 instance の資材は触らず「触らない」として表示する (拒否ではなく除外)。`.env` は既定で消さず (`--include-env` のときだけ)、linger はウィザードが有効化した所有記録があり別の確認 (`linger`、非端末は `--include-linger`) を通したときだけ解除する (手動で有効化された linger は `--include-linger` でも変更しない。記録は linger の処理後に消す)。追跡ファイルと `~/.config/agentic-fx/` 以外の `~/.config` に触れない。対象は固定の相対パス表から求め、symlink で root の外へ出るものは辿らない。 |

### 8.2 受入条件

| ID | 条件 | 観測 |
|---|---|---|
| AC-1 | 初回 (空の root = example のみ): 段 3 は**既定が無いので分岐を明示的に選ぶ**。その他の段は既定で答える。下の 3 シナリオそれぞれで settings.yaml・DB・状態・確認記録ができ、`initialized=true` で起動ガードを通る | tmp root で `ensure_initialized` が通る。settings.yaml と example の diff が回答した行だけ |
| AC-1a | 分岐 1 (接続済み): fake bridge が成功する。`mt5` のみ enabled、`eval_source: mt5`、`price: ok` | settings 差分と記録 |
| AC-1b | 分岐 2 (未準備): 接続試験なし。`mt5` のみ enabled、`eval_source: mt5`、`price: planned`、`bridge_plan: planned` | 同上 |
| AC-1c | 分岐 3 (使わない): yfinance。`eval_source: none`、strategy gate は `history_source_unconfigured` で止まる、`afx history import` は source 未指定を推測しない、status に履歴源の設定手順が出る | 同上 + gate と import の単体 |
| AC-2 | 段 0〜9 の各所で中断すると、`data/` 配下の lock ファイル (`instance.lock`、`ops/setup.lock`) 以外のファイルが増えない・変わらない | 中断前後の root のファイル一覧とハッシュ |
| AC-3 | 再実行: 既存 settings.yaml (コメント・独自の桁揃え・example に無い順序を含む) に 1 項目だけ答えると、diff はその 1 行 | 行単位 diff |
| AC-4 | 重複キーのある settings.yaml で再実行すると、何も書かずに該当キーと行番号を表示して rc=1 | 出力と root 不変 |
| AC-5 | 書き換えが意味的に壊れる形 (複数行スカラー等) では書かずに「手で編集してください」と出る | 付録 A の folded scalar 例で root 不変 |
| AC-6 | instance lock を別プロセスが持つと、段 0 で rc=2・何も書かない。`--non-interactive` も同じ | 子プロセスで lock を保持して実行 |
| AC-7 | 非 tty・フラグ無しで rc=2、文面に `--non-interactive` が含まれる | stdin をパイプにして実行 |
| AC-8 | 価格源 3 分岐のそれぞれで、enabled が選んだ源 1 つだけになり、`Settings` 検証で `primary` が選んだ源に導出される | 書き出した settings を検証 |
| AC-9 | 分岐 2 (未接続) で完了した root からサービスを起動すると、data state が `degraded` になり取引判断 mission が起動しない。fake bridge を繋ぐと足が取り込まれ `ready_confirm_ticks` 後に `ready` | fake clock + fake fetch の結合テスト |
| AC-10 | 「待つ」を選ぶと、直近の期に `expected=0`・slot 0 件の wave 行が 1 行でき、supervisor の tick がその期に mission を起動しない。次の期では起動する | supervisor の tick を実時刻の例で 2 回 |
| AC-11 | 「今すぐ」を選ぶと wave 行を作らず、supervisor の初回 tick が起動する | 同上 |
| AC-12 | 期の表示と supervisor の期が一致する: Asia/Tokyo weekly `Sat 03:00` で 2026-10-02 15:00 JST / 2026-10-03 03:00 JST ちょうど / 2026-10-03 10:00 JST、daily `03:00`、および夏時間の切り替わる TZ (America/New_York、2026-11-01 の `Sun 01:30` — 01:30 が 2 回ある日) | 表示の期キー == supervisor の期キー |
| AC-13 | 標準指標: 一覧で `n` と答えると bless が 0 回。`y` なら選んだ本数だけ bless が呼ばれ、approval の decided_by が人の経路。1 本の gate 失敗で残りが続く | bless を spy |
| AC-14 | ウィザードの全出力・確認記録・unit・settings.yaml に、テスト用に仕込んだ秘密の値 (環境変数と `.env`) が現れない。ウィザードは ops の IO を作らず渡さない。fake の `interactive_enable` はウィザードから io を受け取らない。T9 のテストでは、token / webhook URL の目印を secret 入力の差し込み口にだけ渡し、ウィザードの出力・記録・子 process の argv / env / stdin に現れない | 目印文字列の全文検索と、fake の引数の検査 |
| AC-15 | 生成した unit に作者固有のパス・nvm のバージョン文字列が含まれず、`WorkingDirectory` がテストの root、`PATH` が解決した bin の親を含む | unit の本文 |
| AC-16 | `afx check` の fatal: settings の構文不正・mission ceiling 違反・codex の実行ファイルが ELF でない・検査⑤ に該当する秘密 env がある環境では、unit を置かず理由を出す。fatal の分類は `afx check` と service の起動前検査で同じ関数 | fake systemctl の呼び出し 0 回、unit 不在 |
| AC-16a | `afx check` の warning のみ: PATH に claude が無い (`bin_not_found`)・認証ファイルが無い (`credentials_missing`)・`deferred_by_user`・`user_declined_probe`・モデル未確認のとき、`afx check` は rc=0 で warning を一覧し、unit は設置される。その unit で service を起動すると、起動拒否にならず degraded で起動し、取引判断 mission は fail closed で止まり、未完の手順に LLM 未検証が出る | fake systemctl の呼び出し記録、service を起動する結合テスト、status |
| AC-17 | 引き渡しの順序: 外から見る手段を選ぶと、呼び出し順の記録が「unit 設置 → handoff の marker 設置 → instance.lock 解放 → `interactive_enable(lease)` → marker 除去 → (起動直前に instance.lock を取って即解放) → start」になる。enable の呼び出しの時点で setup.lock は保持され、instance.lock は非ブロッキングで取れる。lease を持たない `afx ops enable` は setup.lock で止まる。「あとで」なら marker は置かれず、instance.lock の解放は `start` の直前のみ | fake systemctl と fake enable の中で lock を取得する |
| AC-18 | 既存 unit がある再実行で差分が表示され、`N` なら unit は変わらない。`y` なら `.bak-*` が残る | ファイル比較 |
| AC-19 | `afx ctl status` と splash の未完の手順: 記録なし・LLM 未確認・bridge 未接続・`.env` 不足 (キー名のみ)・標準指標未導入・戦略なし・履歴源なし (`none`)・履歴なし・ops 未了・ready 未確認・隔離不適合・API 無効・API 起動確認失敗・起動未了の各条件で該当行が出て、全部満たすと節自体が出ない。記録が壊れていても status は例外にならない (`record_state=corrupt`)。API 無効と API 起動確認失敗の行は `afx ctl status` に出ず splash とウィザードだけに出る | status provider と splash 生成の単体 |
| AC-20 | `--non-interactive` は従来の init の既存テスト (`tests/test_init_and_guard.py`) を、呼び出しをフラグつきに変えるだけで通す | 既存スイート |
| AC-21 | 回帰: init・service・commands・improve supervisor の既存スイート | フルスイート |
| AC-22 | 生成した unit に `Type=exec`、`ExecStart=<root>/.venv/bin/python main.py --daemon`、`Delegate=pids memory`、`RestartPreventExitStatus=75 76`、`TimeoutStopSec=630` があり、`ExecStop`・`screen`・`afx-start` を含まない。`.venv/bin/python` が無いとき・root に systemd で書けない文字があるときは unit を置かない | unit の本文。fake systemctl の呼び出し 0 回 |
| AC-23 | 外から見る手段を「あとで」(既定) にすると、`afx ops enable` の呼び出しが 0 回で、`afx-ops@.service` と drop-in が作られない。feature を選ぶと選んだ数だけ呼ばれ、戻り値が 0 でなくてもウィザードは完了し、確認記録に `ops.<feature>=failed` と固定 reason が残る | fake enable の呼び出し記録 |
| AC-24 | afx-ops の起動コマンドが解決できない環境では enable を呼ばず、`later` に倒して案内を出す。unit は置かれ、完了する | `PATH` と module 探索を差し替え |
| AC-25 | 操作 API の回答が「はい」なら settings.yaml の差分は `api.enabled` の 1 行。「いいえ」なら変わらない。unit 設置済みで無効のとき、未完の手順に出る | 行単位 diff |
| AC-26 | 起動後の journal 行 `improve isolation: enabled; backend=… admitted=yes` または `admitted=no reason=…` を、今回の `InvocationID` (または起動前の cursor 以降) の行だけから、現在の改善 backend と一致するものを選んで `isolation` に記録する。前回の起動の `admitted=yes` が journal に残っていても今回の結果にならない。行が複数なら今回分の最後の妥当な行。journal 不可 (`journal_unavailable`)・形式不正 (`journal_format_invalid`)・行が出ない (`admission_line_timeout`、待ち続けず案内で終える) は別 reason | 注入した journal 出力で 6 通り (yes / no / 前回分のみ / 複数 / 不可・不正 / 無し) |
| AC-27 | 確定 1 の crash matrix (§4.3): 各中断点 (marker 前・marker 後 settings 前・settings 後・`init_db` 中・transaction 中・`setup.json` 後・`app_state.json` 後・marker 除去前) で処理を落とし、(a) marker が在る間はサービスが 76 で起動せず `Restart` されない、(b) 再実行が収束し marker が消える、(c) bak は最初の 1 つだけ、(d) 収束後の実物が一度で成功した場合と一致する。加えて起動ガード: settings.yaml が壊れている・新旧の schema が混在している・空であるときも、marker が在れば終了コード 76 (75 の marker も在れば 75) で、`ensure_initialized`・`.env` 読み込み・`load_settings` を呼ばない (呼び出し記録 0 回)。marker の検査が `ENOENT` 以外の失敗 (権限など) でも「在る」として止まる。早期ガードと `build_app` の lock 取得の間に marker が置かれる競合 (取得前・取得直後の再確認) | 注入点ごとの fault injection と、壊れた settings での起動 |
| AC-28 | 引き渡しの crash matrix (§4.3): 各中断点で落とし、marker と stage の組から再開地点が決まって収束する。enroll 中 (75) と handoff 中 (76) が同時に在るとき 75 が先 | 注入点ごと |
| AC-29 | 競合 (§4.2 (a)〜(e)): 同時の `afx init`、ウィザード中の手動起動、handoff 中の systemd start と手動起動、handoff 中の別の `afx ops enable` が、どれも設定・DB を壊さず、固定の rc / reason で止まる | 子 process と fake systemctl |
| AC-30 | `backtest.eval_source: none` が `Settings` 検証を通り、`HistoryDataset` の検査が `none` を受け、import の選択肢に `none` が無い。example の既定が `none` | 設定の検証 |
| AC-31 | 起動後確認 3 段階 (§6.4): (a) active・(b) 認証つき status 成功・(c) ops ready を別々に記録する。`start_ops_api` が失敗したサービスで (a) は成功・(b) は `api.result=failed`、完了する。API 無効のとき (b) は `disabled` で、案内が splash とウィザードだけに出る | fake と実 ApiServer |
| AC-32 | settings の migration: 旧 example (各世代) から作った settings.yaml を移行すると、新規必須キーが所定の block に example のコメントつきで入り、触らない既存の行が byte 一致。未知の手編集の形・新しすぎる版・`--non-interactive` では何も書かず、手修正の案内と rc=1。schema を変える変更の完了条件 (§4.4 の 5 点) を、旧 example fixture が現行 example の `schema_version` ぶん揃っていることの test で強制する | 行単位 diff |
| AC-33 | status 応答の setup 節: 固定 schema (record_state・pending_steps) を満たし、URL・例外文・パス・秘密値の目印を仕込んでも応答に現れない。未知の code・severity を client が前方互換で表示する。権限は status.read のみ | status provider と client の単体 |
| AC-34 | T9 側の変更 (T11): `interactive_enable` が lease を受け取り、違う root・解放済みなら `setup_lease_invalid`。戻り値が `EnableResult(rc, stage, reason_code)` で、reason を stdout / stderr の文面から得ていない。lease 無しの既存経路 (`afx ops enable`) は従前どおり | T9 の既存試験 + 追加 |
| AC-35 | 実機受入の手順書 `docs/operations/first-run-acceptance.md` が §8.4 の全項目 (主シナリオ、追加シナリオ、試験環境の都合、リセットと自動化) を持つ。手順書だけで clean clone から立ち上げられることを別マシンで確かめる (受入の本体) | 実機の受入記録 |
| AC-36 | `scripts/first-run-reset.sh` の製品契約: wizard / daemon が所有する設定・データ・unit・鍵 (AC-37 の対象) が取り除かれ、`config/settings.yaml.example`・`.gitkeep`・追跡ファイルは残る。`--include-env` を付けると `.env` も消え、付けなければ `.env` は残る。venv が無い・settings.yaml が壊れている root でも動く。`__pycache__/` や `.venv/` など、所有物でないものを取り除くことは契約に含まれない | tmp clone 上の実行 |
| AC-36a | 試験の clean 判定: 実 git の tmp clone で、wizard 実行前の `git status --porcelain=v1 -z --ignored=matching` を baseline に保存し、ウィザード (fake の systemctl と enable) を最後まで通して reset した後の同じ出力と、NUL 区切りの repo 相対 path の集合で比較する。baseline に無い path が固定集合 (`.env` (`--include-env` を付けなかったとき)・`.pytest_cache/`・`.ruff_cache/`・`.venv/`・`afx-ops/.venv/`) の外にあれば失敗する (深い階層の未知の ignored ファイルを仕込むと失敗する)。ディレクトリ単位で許可した path の内部は検査しない。`PYTHONDONTWRITEBYTECODE=1` の有無の両方で実行し、無しの場合の `__pycache__/` は baseline に含まれる | tmp clone 上の実行 |
| AC-37 | reset の対象: 当 instance の unit (`afx.service`、`afx-ops@.service`、drop-in) と `.bak-*`、当 instance の `~/.config/agentic-fx/api/<instance_id>/`、`instance_root` が当 root と一致する feature の config・secret、`config/settings.yaml` と `.bak-*`、`data/`、`logs/`、`plugins/` 配下、`policy/directives.md` が消え、`config/settings.yaml.example`・`.gitkeep`・`docs/examples/plugins/`・`.venv/` は残る。**別 instance の資材が不変**: `instance_root` が別の root の feature の config・secret・state、別の `<instance_id>` の鍵ディレクトリ、`WorkingDirectory=` が別の `afx.service`、`instance_root` の canonical な行が 1 件でない (読めない・形が違う・手編集・重複) feature は、バイトもパーミッションも不変で、「触らない」として表示される。`~/.config/agentic-fx/` 以外の `~/.config` は不変。対象表示に「paper 取引履歴、注文、approval、改善 report、RAG、plugins を永久削除」の文が出る | ファイル一覧とハッシュ。別 instance 分は同じ tmp HOME に foreign の資材を置いて前後比較。`\` や `"` を含む root の canonical 行が writer の出力と一致すること、`XDG_CONFIG_HOME` が絶対パスのときの ops の base、API 鍵の base が常に `$HOME/.config` であることも照合する |
| AC-38 | reset の確認: 対象の全数が表示され、`reset` の打ち込みが無ければ何も消えない。非端末で `--yes` 無しは rc=2 で何も消えない。`--dry-run` は表示のみ。表示に除外した別 instance の資材が別枠で並ぶ | fake TTY と非端末 |
| AC-39 | reset の稼働中保護: instance.lock の保持者が居る、または当 instance の unit が active のとき、`reset` の確認だけでは停止も削除もされず、別の `stop` (非端末は `--stop-running`) が要る。手動起動の保持者は止めずに rc=2。linger: ウィザードの所有記録 (`owned_by_wizard`) があり別の確認 `linger` (非端末は `--include-linger`) を通したときだけ `loginctl disable-linger` を呼び、その後に記録が消える。記録が無い・壊れている・`already_enabled`・`failed` のときは、`--include-linger` を付けても `loginctl disable-linger` を呼ばない (fake の呼び出し記録 0 回)。`data/` の削除は linger の処理より後。**反復の再現**: 1 回目のウィザードが linger を所有 (`owned_by_wizard`) → 非端末の `scripts/first-run-reset.sh --yes --stop-running --include-linger` → 2 回目のウィザードでも、初回と同じ linger の状態 (`Linger=no` から始まり、所有記録が `owned_by_wizard` になる) が再現される。`--include-linger` は解除の許可であって所有判定の代替ではない | 子 process と fake systemctl / loginctl の呼び出し記録 |
| AC-40 | reset の冪等と安全: 途中で落としても再実行で収束する (`afx.service` が最後まで残り、所有の判定が再実行でも変わらない)。何も無い root でも rc=0。root の外へ向く symlink は辿らず対象から外して表示する。reset 後の `afx init` が初回として通る。foreign の `instance_root` と別 instance の鍵・config が不変 (AC-37 と同じ前後比較を、途中で落とした後の再実行でも行う) | fault injection と再実行 |
| AC-41 | 段 4 の local LLM 無し (主シナリオ): fake の llama-swap が応答しないと、既定を置かず「Claude を使う / 改善だけ Codex / 別ホストの llama-swap / 後で設定する」を明示的に選ばせる。応答すれば両方 `local` が既定。claude を選ぶと `runner.{trade,improve}.backend: claude`。CLI 未導入 (`bin_not_found`)・未ログイン (`credentials_missing`)・Node 無しでも完了し、記録に固定 reason が残り、未完の手順に手順書の節名つきで出る。「後で設定する」は `runner.*` を変えず `deferred_by_user`、`afx check` は warning で unit は設置される (AC-16a)。別ホストの URL は `/models` の成功・接続不可・401/403 (`auth_unsupported`)・一覧が空 (`model_not_found`) を区別する | fake の llama-swap と PATH の差し替え |
| AC-41a | Claude 選択時の消費確認 (T5a): 「trade 用の `claude -p` を 1 往復で試験します」の確認が出る (既定 N)。同意すると fake の `claude` が trade 用の呼び出しをちょうど 1 回受け、結果が `llm_trade` に記録される。同意しない (Enter) と呼び出し 0 回で `llm_trade: unverified`、reason `user_declined_probe`、完了し、未完の手順に出る。local / codex だけを選んだ場合は確認が出ない | fake の `claude` の呼び出し記録 |
| AC-43 | example の LLM 未設定値の扱い (T1 / T2 / T7a で確定した方式): 「後で設定する」の settings が `Settings` の検証を通るか、通らないなら `afx check` と service が fatal ではなく warning として扱うこと。確定した方式で `Settings` と `afx check` の受入試験が同じ task に入っている | `Settings` の検証、`afx check` の出力 |
| AC-42 | journal の観測契約 (§6.4): 起動前の InvocationID と cursor を取り、前回と異なる空でない InvocationID を今回の起動とする。`_SYSTEMD_INVOCATION_ID` で絞る / 無ければ `USER_INVOCATION_ID` で絞る / どちらの field も無いときだけ cursor 以降に切り替え、方式が記録に残る。前回の起動の `admitted=yes` が journal に残っていても今回の結果にならない | 注入した journalctl 出力 (JSON) で 5 通り |

### 8.3 テスト方針

- **対話は入出力を差し替える**: ウィザードの IO は `WizardIO` で、`isatty()`・`ask(prompt) -> str`・`say(text)` を持つ。試験の fake TTY は台本 (回答の列) と出力の記録に加え、`isatty` の真偽、`ask` が EOF (入力の終わり) を返す場合、`KeyboardInterrupt` を投げる場合を含む。既存 `shell.py` の `prompt_fn` / `input_fn` の作法 (inventory §9) に合わせる。ウィザードは秘密を受けないので `getpass` も `ask_secret` も使わない。ops 側の `ask_secret` の差し込み口は T9 のテストだけに置く。
- **実環境に触れない仕切り**:
  - root は常に `tmp_path`。`entry.main` 経由のテストは cwd を tmp に移す。
  - systemd と ops: unit の置き場 (`XDG_CONFIG_HOME`) と `systemctl` / `loginctl` / `journalctl` を呼ぶ関数、`afx ops enable` を呼ぶ関数を注入。実 `systemctl` も実 `afx ops enable` も呼ばない。既存の session ガード (`tests/conftest.py`、memory `tests-touching-real-repo-resources`) に「`~/.config/systemd/user` への書き込み」「`~/.config/agentic-fx` への書き込み」「`systemctl` の実行」を検出するガードを足す。
  - ネットワーク: llama-swap・bridge・yfinance への試験は関数として注入 (既存の conftest は外部アクセスをモック)。
  - 実 `config/settings.yaml`・実 `data/`・実 unit を読む/書くテストは作らない。
- **フィクスチャは実物から**: bridge の `/health`・`/quote` の応答、llama-swap の `/models` の応答は、実物の形 (`mt5_bridge/server.py` のモデル、`service.py:162-171` が読むキー) から作る (memory `test-fixtures-from-real-transcripts`)。
- **時刻**: 期の計算と次回予定の表示は、AC-12 の実時刻の例で pin する (memory `time-contracts-need-wallclock-integration-tests`)。夏時間の切り替わりを 1 例含める。
- **crash の注入**: 確定 1 の各書き込みの前後と、引き渡しの各手順の前後に fault injection の点を置く (marker・`os.replace`・transaction の commit・unlink)。点ごとに「落とす → 状態の検査 → 再実行 → 収束の検査」を行う (AC-27・AC-28)。fsync の呼び出しは spy で確かめる。
- **リセット**: スクリプトを実 git の tmp clone に対して実行し、製品契約 (AC-36) と、ウィザード実行前の baseline との完全比較 (AC-36a) で確かめる。`HOME` と `XDG_CONFIG_HOME` を tmp に向け、`PATH` の先頭に fake の `systemctl` / `loginctl` を置く。同じ tmp HOME に別 instance の資材 (別 `instance_root` の feature、別 `<instance_id>` の鍵、別 `WorkingDirectory` の unit) を置いて前後比較する。スクリプトの所有規則 (unit の置き場、`instance_id`、`ops.toml` の書式) を、ウィザードと afx-ops が実際に置く場所と同じ tmp 環境で照合する試験を持つ (規則の食い違いを検出する)。bash は `shellcheck` を通す。
- **変異の観点 (段 0 用のメモ)**: IV-2 (他の行を 1 文字変える変異で AC-3 が落ちるか)、IV-4 (他の源を true のまま残す変異)、IV-9 (lock 解放を起動の後に回す変異で AC-17 が落ちるか)、IV-13 (印を `expected=1` にする変異で AC-10 の「failed slot が残らない」が落ちるか — この assert を AC-10 に含める)、IV-19 (marker を置かない・早く消す変異で AC-27 が落ちるか)、IV-20 (lock の取得順序を逆にする変異)、IV-22 (cursor・InvocationID で絞らない変異で AC-26 の「前回分のみ」が落ちるか)、IV-25・IV-26 (確認を飛ばす・`.env` を既定で消す・symlink を辿る変異)。

### 8.4 実機受入 (自動試験では確かめられないもの)

方針 (2026-10-12 裁定): 別マシンで、**Claude の記憶に頼らず repo の案内だけで** clean clone から立ち上げる。主は Ubuntu 22 の実機。WSL は対話の流れの確認用で、unit と隔離の判定には使わない。足りない知識が出たら案内の欠陥として直す。このマシンの別 user は速い反復用で、最終受入にしない。やり直しは独立スクリプト `scripts/first-run-reset.sh` (§4.5) で clean clone の状態に戻して繰り返す。

受入手順書 `docs/operations/first-run-acceptance.md` を、この束の成果物 (T14) にする。内容: (1) 前提 (OS、user systemd、cgroup v2、`uv`、clone 先、LLM と bridge の有無。**CLI の導入節を含む**: Node.js の導入確認、Claude CLI の導入、`claude` のログイン、`claude --version`、`claude -p` の最小確認、改善を Codex にするなら Codex CLI の導入・ログイン・実体 path)、(2) 手順 (clone → `uv sync` → `afx init` → 常駐化 → ops → 起動 → 確認 → 必要なら `scripts/first-run-reset.sh` で最初から)、(3) 各段で見える文面と期待、(4) 詰まったときに貼ってもらう情報 (次の表の観測コマンドの出力、`journalctl --user -u afx -n 100`、`data/state/setup.json` の固定語彙だけの抜粋。秘密と URL は貼らない)。

| 項目 | 観測 | 期待 |
|---|---|---|
| 実効の unit | `systemctl --user cat afx.service`、`systemctl --user show afx.service -p Type -p NotifyAccess -p KillMode -p DelegateControllers -p MainPID -p TimeoutStopUSec -p SendSIGKILL -p FinalKillSignal -p TimeoutStopFailureMode -p RestartPreventExitStatus -p Restart -p RestartUSec` | 設置した本文と一致。`Type=exec`、`NotifyAccess=none`、`KillMode=control-group`、`DelegateControllers` に `pids` と `memory`、`MainPID` が `python main.py --daemon`、`TimeoutStopUSec=10min 30s`、`RestartPreventExitStatus=75 76` |
| cgroup の形 | `/proc/<MainPID>/cgroup` と mission 実行中の cgroup | afx が unit root から `supervisor/` へ自己移動した位置にあり、mission ごとの cgroup が作られる |
| linger とログアウト後の存続 | `loginctl show-user $USER -p Linger`、ログアウト後に再接続して `systemctl --user is-active afx` | `Linger=yes` なら存続。`no` ならログアウトで止まり、ウィザードが案内を出していたこと |
| 今回の起動の journal | §6.4 の観測契約どおり: 起動前後の `systemctl --user show afx.service -p InvocationID --value`、`journalctl --user -u afx.service -o json` の `_SYSTEMD_INVOCATION_ID` / `USER_INVOCATION_ID` の実在、cursor の fallback。Ubuntu 22 と systemd 有効の WSL の両方で field を確かめる | 今回の起動の admission 行が読め、前回の起動の行と区別できる。`admitted=yes`。field が契約と違えば契約の field 名を直す |
| 終了コード 75 / 76 の非再起動 | marker を置いた状態で `systemctl --user start afx`、`show -p ExecMainStatus -p NRestarts -p Result` | 終了コード 76 (enroll 中なら 75)、`NRestarts=0`、再起動されない |
| 依存の実効値 | `systemctl --user show afx.service -p Wants -p After`、`afx-ops@<feature>.service` の `-p Wants -p Requisite -p PartOf -p After` | afx-ops spec §6.2 の表と一致 |
| ops の ready と API 認証 | `afx-ops@<feature>` の active と READY、`afx ctl status` の成功、鍵なし / 誤りの鍵での失敗 | READY は操作 API の認証後。鍵なしは拒否 |
| ops の restart 連動 | `systemctl --user restart afx`、`stop afx` | ops が追随して ready に戻る / 一緒に止まる |
| リセットからのやり直し | `scripts/first-run-reset.sh` → `git status --porcelain --ignored` → 再び `afx init` | clean clone と同じ (AC-36)。再実行も同じ流れで通る。別 instance の資材があれば触らず表示される |
| 環境差 | Ubuntu 22 (主) / systemd 有効の WSL / systemd 無効の WSL | Ubuntu 22: 全項目が期待どおり。systemd 有効の WSL: 対話の流れを確かめる用 (`Delegate` が効かず admission が `cgroup_topology_unsupported` でも、それは環境差であって失敗ではない)。systemd 無効の WSL: `systemctl --user` が使えず、§6.2 の 3 により unit を置かず手動起動の案内で完了する |


**主シナリオと追加シナリオ** (手順書はこの順で書く。ウィザードの文面と期待は段 4 を正とする):
1. **主シナリオ = local LLM が無い Ubuntu 22**: 取引判断は `claude -p`、改善は Claude。前提節の手順 (Node 導入確認 → Claude CLI 導入 → `claude` ログイン → `claude --version` → `claude -p` の最小確認) を通してから `afx init` に入る。段 4 で llama-swap が応答しないので既定が出ず、「Claude を使う」を選ぶ操作と期待する表示 (消費確認に同意すると trade 用の `claude -p` を 1 往復する) を手順書に書く。完了後、`afx check` が通り、unit が置かれ、起動後確認 3 段階が通る。
2. **追加: 改善を Codex にする**: Codex CLI の導入・ログイン・実体 (ELF) の絶対 path。見つからないときの `bin_not_found` と案内文を確かめる。
3. **追加: 別ホストの llama-swap**: URL を入れて `/models` の成功を確かめる。URL の誤り (接続不可)、認証を要する URL (`auth_unsupported`、認証不要が前提)、空の一覧を確かめる。
4. **追加: CLI 未導入・未ログインで完了する**: Claude CLI を PATH から外した状態、ログイン前の状態で `afx init` を最後まで通し、`bin_not_found` / `credentials_missing` が記録に残り、未完の手順が手順書の節名つきで出ること、`afx check` が warning で unit は設置され、起動後は degraded と未完の手順が表示されることを確かめる。
5. **ウィザードが `local` を既定にしない**ことの確認: 主シナリオでは llama-swap が無いので段 4 に既定が出ないこと、操作者があえて local を選んでもモデル一覧が取れず `unverified` で進み、未完の手順に出ることを確かめる。

**試験環境の都合** (この節は受入手順書の別節に分け、利用者向けの本文・README には入れない):
- サブマシン (Ubuntu 22) には local LLM と MT5 が無い。本環境の bridge (:8812) と llama-swap (:8080) を SSH トンネル `ssh -N -L 8812:127.0.0.1:8812 -L 8080:127.0.0.1:8080 <本環境のホスト>` でサブマシンの localhost に見せる。トンネルは user unit (`Restart=always`、`ServerAliveInterval` つき) で常駐させ、製品側の既定 URL (`http://localhost:8812`、`http://localhost:8080/v1`) をそのまま使う。製品のコードと設定の既定はトンネルの存在を知らない。
- **トンネルは主シナリオを歪める**: :8080 を転送していると llama-swap が応答するため、段 4 の既定が `local` になり、「local 無し」の分岐を通らない。主シナリオ (上の 1〜5) は :8080 の転送を止めて行い、追加シナリオ 3 (別ホストの llama-swap) と、MT5 bridge を使う分岐 1 の確認は転送を有効にして行う。
- トンネルで本環境の bridge に繋ぐので、サブマシン側から本環境へ発注系の endpoint が届く。受入は価格の読み取りだけを行い、`DRY_RUN` の設定を変えない。

**リセットと自動化の範囲**:
- **1 回目の受入は人の手で**手順書どおりに行う (案内の欠陥を見つけるのが目的なので自動化しない)。
- 2 回目以降は SSH 経由で自動化する: `scripts/first-run-reset.sh --yes --stop-running --include-linger` でリセット (`--include-linger` は解除の許可であって所有判定の代替ではない。ウィザードの所有記録 `owned_by_wizard` が無ければ何も変えない。これを付けないと、1 回目に所有した linger が残り、2 回目のウィザードが `already_enabled` と記録して所有を失うので、以後のリセットで解除できなくなる) → 対話の driver (expect 相当。台本の回答を送り、出力を記録する) で `afx init` を通す → 手順書の観測コマンドを束ねた診断収集で出力を回収する。driver と診断収集は**リポジトリの成果物として作る**(T14 が所有する。例: `tests/acceptance/first_run_driver.py`、`scripts/collect-first-run-diagnostics.sh`。最終の配置名は T14 内で決めてよいが、「作るかどうか」は未決に戻さない)。秘密と URL は回収しない。自動化は回帰の確認用で、案内の評価は 1 回目に行う。

---

## 9. 実装 task の分割案

| task | 単独で入る成果 | 触るファイルの見当 | 依存 | 並列 | 所有する受入条件 |
|---|---|---|---|---|---|
| T1 設定の書き換え器 | 行単位の書き換え + G1〜G3 + `schema_version` と版ごとの migration (所定 block への行挿入・rename・削除。未知の形は変更せず手修正を案内。§4.4)。**完了条件**: settings schema を変える変更は、同じ変更で version bump・前版からの migration・旧 example fixture・byte 一致の test・理由を揃える (§4.4 の運用責任。この条件を満たすことを検査する test も T1 に含める) | 新規 `src/agentic_fx/setup/settings_edit.py`、`settings_migrate.py`、tests。example の「後で設定する」の未設定値の方式 (架空モデル名を残す / 明示的な未設定値を書く) を T2・T7a と合わせて確定する | なし | 可 | AC-3〜5、AC-32、AC-43 (未設定値の方式の確定。T2・T7a と共有) |
| T2 検査部品の構造化 | llama-swap 確認を結果の構造 + 文面に分離 (現行 init の出力は不変)、bridge `/health` と quote の試験、CLI backend 検査の再利用口、`.env` キーの有無、Landlock・pytest の有無、afx-ops の解決可否 | `service.py` (`_check_llama_swap` の分割)、新規 `setup/checks.py`、`datafeed/sources.py` (health 関数の追加のみ)、tests | なし | 可 | 段 4 の probe 結果型 (llama-swap の結果の構造化、消費確認つき trade 用 `claude -p` 1 往復の部品)、AC-41 の backend 検査部分、AC-43 (共有) |
| T3 確認記録と未完の手順 | `setup.json` の読み書き (fail-soft、原子的置換)、splash の「未完の手順」節 (code から文面を生成する renderer。§7.3 の固定 schema) | 新規 `store/setup_record.py`、`commands.py`、`service.py` (splash)、tests | なし (記録の形を先に固定) | 可 | AC-19 (未完の手順の生成と splash) |
| T4 期の消費印 | `improve_waves` に `expected=0` の行を置く関数 + supervisor が起動しないことの pin + 他の読み手に出ないことの pin | `store/improve_waves.py`、tests (`tests/store/test_improve_waves.py`、`tests/core/test_improve_supervisor.py`) | なし | 可 | AC-10、AC-11、AC-12 |
| T5a ウィザードの本体 | 段 0〜10・12・14 の質問・要約・commit orchestration (段 8 の操作 API と `.env`、段 13 の引き渡しの呼び出し順を含む。確定 1 の commit protocol・lock・marker は T10 の部品を使う。unit と ops の実体は T7a / T7b)、再実行、排他、段 4 の local 無しの分岐 (§3 段 4)、`entry.py` の `init` の差し替えと help の更新 | 新規 `setup/wizard.py`、`entry.py`、tests (台本 IO) | T1〜T4・T5b・T10〜T13 | 不可 (統合) | AC-1、AC-1a〜b、AC-2、AC-6〜9、AC-14、AC-25、AC-41 の対話の流れ、AC-41a |
| T5b 既存 init の `--non-interactive` 移設 | 従来の `run_init` を `--non-interactive` として保つ (挙動不変)。同じ 2 つの lock と §4.3 の marker を入れ、確認記録を `mode: non-interactive` で書き、`--config <file>` を受ける。版が古い settings は理由と `afx init` を案内して rc=1 (migration しない) | `service.py` (`run_init` を非対話側へ)、`entry.py` の分岐、`tests/test_init_and_guard.py` の呼び出し変更 (AC-20) | T3・T10 | T1〜T4 と並行可 (T5a の前に入れる) | AC-20、AC-21 |
| T6 標準指標の段 | 一覧・除外・確認・bless の逐次実行・失敗文面 | `setup/indicators.py`、`plugin/switch.py` は呼ぶだけ、tests | T5a の差し口 (台本 IO) | T5a と並行で書けるが統合は T5a の後 | AC-13 |
| T7a unit の生成と設置、`afx check` | direct exec の unit 生成 (固定行の写しと値の検出)・差分・退避・systemctl/loginctl 注入、linger の確認つき有効化と所有記録 (§3 段 13 手順 6 (a))、`afx check` (設定と実行環境だけの起動前検査、§6.2。結果を fatal と warning に分ける分類関数を service の起動前検査と共有し、warning (LLM 未検証) だけなら service も起動拒否にせず degraded で起動する。例: CLI の `bin_not_found` / `credentials_missing`) | 新規 `setup/unit.py`、`entry.py` (`check`)、`service.py` (検査の切り出し)、tests + conftest ガード | T2 (検査部品)、T3 (確認記録) | T5b・T11 と並行 | AC-15、AC-16、AC-16a、AC-18、AC-22、AC-43 (共有)。`afx check` の fatal / warning 分類 (service の起動前検査と共有) |
| T7b ops の呼び出しと起動後観測 | afx-ops 解決の事前検査と `afx ops enable <feature>` の呼び出し (同一 process で T11 の後の `interactive_enable(root, feature, lease=…)` を呼び、戻り値 `EnableResult(rc, stage, reason_code)` を読む。ウィザードは io を渡さない。子 process にしない)、起動後確認の 3 段階 (§6.4)、journal の admission 行の読み取り (**§6.4 の観測契約を着手前に固定してある前提**: InvocationID と `_SYSTEMD_INVOCATION_ID` / `USER_INVOCATION_ID`、cursor の fallback。AC-26・AC-42) | `setup/unit.py` の隣に新規 `setup/post_start.py`、tests (journalctl / systemctl を注入) | T7a、T11 | T5a と並行 | AC-17、AC-23、AC-24、AC-26、AC-31、AC-42 |
| T8 文書 | README の入口、設計書 §8「初期起動フロー」と systemd の記述の改訂 (変更履歴表)、`docs/operations/afx-service-2026-09-26.md` (screen 前提) の置き換え、example のコメント (ウィザードが書き換える項目の印、`trade.backend` の文言、`api.enabled` の文言。`api.enabled` の example 既定を `true` に変え、「seccomp が入るまで」のコメントを消す。`config.py` の `ApiSettings` の既定と注釈も揃える)。**完了条件**: example の schema を変えた箇所は、同じ変更で `schema_version`・migration・旧 example fixture・test が揃っていること (§4.4) | `README.md`、`docs/superpowers/specs/2026-07-25-agentic-fx-design.md`、`docs/operations/`、`config/settings.yaml.example` | T5a・T7a | 最後 | 受入条件の所有なし (完了条件は §4.4 の 5 点) |
| T10 commit protocol と排他 | marker (`data/state/setup_in_progress`) の設置・除去・照合、サービス側の起動ガード (`run_service` の root 解決の直後、`ensure_initialized`・`.env`・settings 読み込みより前の 1 箇所で、75 → 76 の順に存在だけで判定。`ENOENT` 以外は fail closed。`build_app` 側の lock 取得の前・直後の再確認は残す。§4.3)、本番で稼働中の unit (75 のみ) の作り直しを受入条件に含める (U-9: 稼働中 unit の作り直し)、setup.lock → instance.lock の取得と引き渡しの部品、crash の注入点 | 新規 `store/setup_marker.py`、`service.py`、`entry.py`、tests (AC-27〜29) | なし | 可 (T5a・T5b が使う) | AC-27〜29。起動ガードと lock 部品 |
| T11 afx-ops T9 の改修 | `interactive_enable` の `lease` 引数、`EnableResult(rc, stage, reason_code)`、`acquire_setup_lease(root)` の切り出し、ops の IO の型を wizard の IO から分離 | `ops_setup/interactive.py`、`ops_setup/stages.py` (変更は最小)、tests (AC-34) | なし (T9 は main 投入済み) | 可 | AC-34 |
| T12 操作 API の status 項目と仕様の改訂 | status 応答の `setup` 節 (固定 schema、権限は status.read)、client の前方互換表示。`docs/superpowers/specs/2026-10-04-ops-api-design.md` の §4.2 行 1・§10・§11 の改訂 (別 task として列挙) | `ops/service.py` の status provider、client (`afx ctl`)、ops-api spec、tests (AC-33) | T3 (記録の形) | T5a と並行 | AC-33 (status の wire contract) |
| T13 履歴源 `none` | `backtest.eval_source` に `none`、`HistoryDataset` の検査、strategy gate の `history_source_unconfigured`、`afx history import` の推測禁止、example の既定 | `config.py`、`backtest/dataset.py`、`plugin/strategy_gate.py`、`plugin/switch.py`、`backtest/cli.py`、`config/settings.yaml.example`、tests (AC-1c、AC-30) | なし | 可 | AC-1c、AC-30 |
| T14 実機受入の手順書 | `docs/operations/first-run-acceptance.md` (§8.4 の前提 (CLI の導入節を含む)・手順・主シナリオと追加シナリオ・各段の文面と期待・「試験環境の都合」節 (SSH トンネル)・詰まったときに貼る情報)。別マシンでの受入結果で改訂する。journal の観測契約 (§6.4) の実機確認を観測表に含める。2 回目以降の自動化の対話 driver と診断収集を repo の成果物として作る (作るかどうかは未決にしない。配置名のみここで決める) | `docs/operations/first-run-acceptance.md`、`tests/acceptance/first_run_driver.py`、`scripts/collect-first-run-diagnostics.sh` (例。配置名は T14 内で確定) | T5a・T7a・T7b・T15b の文面が固まった後 | 最後 (T8 と並行) | AC-35 + driver と診断収集の実ファイル |
| T15a リセットの計画・所有判定・dry-run | `scripts/first-run-reset.sh` の骨格: root の決定、所有規則 (unit の `WorkingDirectory=`、`instance_id` の算出、`ops.toml` の `instance_root` の行読み、repo の固定表)、全数と除外 (別 instance の資材) の表示、対象表示の永久削除の文、`--dry-run`、確認 (`reset`) と非端末の `--yes` の扱い。`Settings`・venv・`agentic_fx` を前提にしない。何も消さない段階までを完成させる | 新規 `scripts/first-run-reset.sh`、tests (AC-37 の表示と別 instance の不変、AC-38) | なし | T5a と並行 | AC-37 (対象の表示と別 instance の不変)、AC-38 |
| T15b リセットの実削除と systemd 停止 | 稼働中の判定と `stop` の確認、`afx-ops@*` と `afx.service` の停止・無効化、unit の削除順 (ops 用 → `afx.service` 最後)、linger の所有記録に基づく解除 (`data/` の削除より前)、`~/.config/agentic-fx/` の当 instance 分と repo の固定表の削除、冪等、`--include-env`、終了時の `git status --porcelain --ignored`。スクリプトの所有規則を、ウィザードと afx-ops が置く場所と照合する試験 | `scripts/first-run-reset.sh`、conftest のガード、tests (AC-36・AC-39・AC-40) | T15a。照合試験は T7a・T11 | T7b と並行 | AC-36、AC-36a、AC-37 (削除の結果)、AC-39、AC-40 |

**共有ファイルの責務 (関数単位)**: 同じ関数を 2 つの task が編集しない。
- `entry.py`: T5a = `init` の対話版への振り分けと help / T5b = `--non-interactive` の分岐 / T7a = `check` サブコマンド / T10 = 終了コード 75・76 の rc 表示のみ。
- `service.py`: T2 = `_check_llama_swap` の分割 (結果型を返す関数) / T3 = splash の「未完の手順」節を呼ぶ部分 / T5b = `run_init` の非対話側への移設 / T7a = 起動前検査の切り出しと fatal・warning の分類関数 / T10 = `run_service` 冒頭の起動ガードと `_acquire_lock_unless_enrolling` の共通 marker 判定への置換。

**実装順序案** (単一 worktree での衝突を抑え、最初に安全境界を固め、最後の統合で未完成の部品を抱えない順序):
T10 → T1 → T3 → T2 → T4 → T13 → T11 → T5b → T7a → T7b → T12 → T5a → T6 → T15a → T15b → T8 → T14
- T10 が最初なのは、起動ガードと marker・lock が全経路の安全境界で、T5a・T5b が使うため。T1 → T3 → T2 → T4 → T13 は、設定・確認記録・probe・消費印・履歴源を独立した部品として固定する。
- T11 は T7b と T5a の引き渡しの前提。T5b を先に入れて互換経路を安定させる。T7a (unit と `afx check`、fatal / warning の分類) を固定してから T7b (起動後の観測) を載せる。
- T15a・T15b は T7a・T11 が実際に作る unit・config との照合試験を含むので、その後。T8 → T14 (文書・手順書・driver・診断収集・実機受入) は最後。

- 実機の受入 (自動テスト外): 詳細は §8.4 と手順書 `docs/operations/first-run-acceptance.md` (T14)。このマシンでは `afx.service` が稼働中で、`afx ops enable` が固定の unit 名 `afx.service` を使うため行わない。別マシン (Ubuntu 22 実機を主、WSL は対話の流れの確認用) で、Claude の記憶に頼らず repo の案内だけで clean clone → `afx init` → 常駐化 → ops 有効化 → 起動 → 起動後確認 3 段階 → admission 行の確認、の順に通す。やり直しは `scripts/first-run-reset.sh` (T15a・T15b)。ticket `first-run-empty-1m-starts-degraded` の確認を兼ねる。

---

## 10. 未決事項と現物の問題

### 10.1 未決事項 (仕分け済み)

番号は索引で、挙動は本文の意味のある名前で決まる。確定済みの実装契約は 10.4 に移した。

**ユーザーの裁定が残るもの**

| ID | 論点 | 推奨と根拠 |
|---|---|---|
|||
**実測後に裁定**

| ID | 論点 | 推奨と根拠 |
|---|---|---|
| U-7 標準指標の段 | 標準指標の段を初回ウィザードで出すか、別コマンドに分けるか | 推奨: 出す (B-9 の裁定どおり)。ただし所要 (9 本 × 最長 300 秒) は未計測で、長いなら「後で」を既定にする余地がある — 実装 task で 1 本の実測を取ってから決める |

**技術調査後に、必要なら裁定**

| ID | 論点 | 推奨と根拠 |
|---|---|---|
| U-8 表示 TZ 変更の追随 | 再実行で `display_timezone` を変えたとき、改善の予定時刻 (表示 TZ で解釈) も動く。そのことを確認文で示すだけでよいか | 推奨: 示すだけ。根拠: 期のキーは UTC の occurrence から作られ (未確認: TZ 変更で期キーが変わるか)、変わる場合は IV-13 の印で起動直後の実行を防げる |

### 10.2 設計中に見つけた現物の問題 (別チケット候補)

| ID | 問題 | 根拠 | 重さの見立て |
|---|---|---|---|
| P-1 | `mt5_bridge/README.md` が「発注 endpoint は意図的に未実装」と書くが、現物の bridge は `POST /order`・`/positions/{ticket}/modify`・`/close`・`/admin/halt` を持つ | `mt5_bridge/server.py:352,431,452,487` | 高 (公開時に誤解を招く。安全側の説明が現物と逆) |
| P-2 | bridge の既定 `BRIDGE_HOST=0.0.0.0` かつ API キー無しは「LAN trust モード」。発注系 endpoint がネットワークに開く既定 | `mt5_bridge/config.py:70-72`、`.env.example` | 高 (`DRY_RUN` の既定は true `config.py:73` だが、既定で LAN に開く形は公開リポジトリの既定として不適) |
| P-3 | `mt5_bridge/README.md` が finance 時代の手順 (`git clone <finance-repo-url>`、`feature/mt5-integration`、Windows main PC と stick PC)、設定例のキー `mt5_bridge:` は agentic-fx では未知キー (`extra="forbid"` で起動拒否) | `mt5_bridge/README.md` 手順節・疎通確認節 | 中 |
| P-4 | `mt5_bridge/.env.example` のコメントに作者の口座の種類 (「OANDA 本番口座 (資金 0 / 発注しない前提)」) と、README にブローカー名・実測環境名 | `.env.example` 冒頭、README サーバ時刻節 | 低〜中 |
| P-5 | `README.md` が 0 バイト | inventory §1.3 | 中 (T8 で入口だけ書く。全体の README は別) |
| P-6 | `improve.research.user_agent` の既定が実在未確認の URL を名乗る | `config.py:319`、example | 中 (外向きリクエストの責任の所在) |
| P-7 | example の `backtest.eval_source: "dukascopy"` が、公開サービスへの大量取得を既定で促す | `settings.yaml.example:43`、memory `dukascopy-likely-blacklisted` | 解消 (v0.3: example の既定を `none` にする。T13) |
| P-8 | 追跡ファイルに作者のホームの絶対パス (`/home/teru358/...`) | inventory C-8 の 5 ファイル | 低 |
| P-9 | `config.load_settings` が `load_dotenv()` を引数なしで呼び、検証のついでに `.env` を環境に載せる。配布形 (非 editable) では `.env` を見つけない可能性 | `config.py:742-743`、inventory C-5。本実測で検証時に実 `.env` が載ることを確認 (付録 A) | 中 |
| P-10 | pyyaml の `safe_load` は重複キーを黙って後勝ちにする。手編集の settings.yaml で、見ている行と効いている値がずれ得る | 付録 A の実測 (UTC / Sun 04:00) | 中 (サービス起動時の検証にも重複検出を入れる価値) |
| P-11 | `settings.yaml.example` の `runner.trade` のコメント「local \| claude」は現物と一致するが、`config.py:115` のパターンは 4 種を受けてから 2 種を拒否する (文言のみ) | inventory §1.4 | 低 |
| P-12 | `improve-catchup-runs-at-startup` は本文が「推定」のまま。機序は現物で確定 (`improve_supervisor.py:96-107`) | 本書 段 7 | 低 (ticket 本文の更新) |
| P-13 | ticket `policy-add-unwired-in-service` は是正済みの現物と食い違う | inventory §1.4 | 低 (ticket 状態の更新) |
| P-14 | `config/settings.yaml.example` と `config.py` の `ApiSettings` の注釈が「改善 worker の seccomp が入るまで既定は無効」のまま。改善 CLI 隔離は有効化済みで、常駐運用は `api.enabled: true` が前提 | `config/settings.yaml.example` の `api:`、`config.py` の `ApiSettings` | 解消 (2026-10-12 裁定: 既定を有効にし、example と `ApiSettings` を T8 で直す) |
| P-15 | `docs/operations/afx-service-2026-09-26.md` と `deploy/afx.service`・`afx-start.sh`・`afx-stop.sh` は screen 前提の仮運用の記述で、本番の direct exec と食い違う | `deploy/` 配下 (未追跡)、同文書 | 低 (T8 で置き換え) |
| P-16 | afx-ops の bot / web / notify の既定 port・設定はウィザード外の `afx ops enable` が決める。旧 `discord.enabled` (`.env` の `DISCORD_WEBHOOK_URL`) と通知が二系統残る | `core/notifier.py`、afx-ops spec §12-3 | 解消 (2026-10-12 裁定: ウィザードは ops の notify だけを案内し、旧簡易通知は別チケット `legacy-discord-webhook-notify-removal` (低) で廃止) |

### 10.3 後回しにする項目

| ID | 項目 | 内容 | 重さの見立て |
|---|---|---|---|
| L-1 | 内蔵の `afx init --reset` (低チケット `afx-init-reset-builtin`) | リセットは独立スクリプト `scripts/first-run-reset.sh` を主にした (§4.5)。内蔵版は、スクリプトの所有規則が安定してから、同じ規則を Python に移すかを決める。起票は実装の時に行う | 低 |
| L-2 | 認証つき llama-swap への対応 | 段 4 は認証不要の URL だけを扱い、401/403 は `auth_unsupported` で止める。認証ヘッダの入力は秘密の扱い (B-13) と衝突するので、`.env` 経由の設計が要る | 低 |

### 10.4 決定記録 (実装契約として確定済み)

2026-10-12 ユーザー承認時に確定 (推奨どおり): U-1 既存利用者 (`initialized=true`、確認記録なし) は起動拒否せず `status` に「確認記録なし」を出す / U-2 `mode` と `autopilot` の初期化は初回だけ (再実行は答えた項目だけ、`--non-interactive` は現行どおり) / U-6 「今すぐ 1 本」を選べる (既定は次の予定まで待つ)。元の表の行:

 U-1 既存利用者の起動拒否 | ウィザード未実施の既存利用者 (`initialized=true`、記録なし) を起動拒否にするか | **拒否しない**、`status` に「確認記録なし」を出すだけ。根拠: 作者の稼働環境を止めない (C-1)。必須化は「初回」に対する方針で、既存環境の再設定は求められていない |
 U-2 mode / autopilot の初期化 | ウィザード完了時に `mode`/`autopilot` を毎回初期化する現行 init の挙動を、対話版では初回だけにしてよいか | **初回だけ**。根拠: 再実行は「答えた項目だけ変える」(B-11) と一貫させる。`--non-interactive` は互換のため現行のまま |
 U-6 「今すぐ 1 本」 | ウィザードが「今すぐ 1 本」を選べるようにすることの是非 (B-8) | 推奨: 選べる (既定は待つ)。根拠: 方針文・課題を段 12 で入れた人はすぐ回したい。印を置かないだけで現行挙動 |

| ID | 論点 | 確定した内容 |
|---|---|---|
| U-3 履歴源 none | MT5 を使わない分岐の `backtest.eval_source` の値 | **解消 (v0.3)**: 明示的な `none` を足し、この分岐では必ず `none` を書く。example の既定も `none` (P-7 も同時に解消) |
| U-4 screen 無し環境 | (v0.1 の論点: `screen` が無い環境で daemon の unit を選べるか) | **解消 (v0.2)**: 常駐は daemon 一本 (B-5 確定)。操作は `afx ctl`、承認は bot か `afx ctl --as approver approve` で足りるので screen は要らない |
| U-5 screen 用スクリプトの置き場 | (v0.1 の論点: screen 用 start/stop スクリプトの置き場) | **解消 (v0.2)**: スクリプトは不要になった。仮運用の `deploy/afx-start.sh` / `afx-stop.sh` / `afx.service` は本番では使われておらず、撤去はユーザー操作 |
| U-9 終了コード 76 と unit の作り直し | サービスの終了コード 76 と unit の `RestartPreventExitStatus=75 76` の導入。本番で稼働中の unit (`~/.config/systemd/user/afx.service`) は 75 だけを持つので、作り直しが要る | **解消 (v0.4): 要裁定ではなく実装・移行の受入条件**。T10 の受入条件に「`RestartPreventExitStatus=75 76` を持たない既存 unit は、marker がある間 30 秒ごとの再起動になるだけで起動しないこと」を、T7a の受入条件に「`afx init` の再実行が差分を見せて unit を作り直すこと」を含める。作者の環境は、次にウィザードを使うときに作り直す (稼働中の unit を手で直すのは利用者の操作) |
| U-10 非対話での古い settings | `--non-interactive` で版が古い settings.yaml を見つけたとき、migration せず rc=1 で `afx init` (対話) を案内する | **解消 (v0.4): 本文の扱いで確定** (§4.4)。無人で既存設定の行を書き換えるのは、答えた行だけ変える規律 (B-2) と相性が悪い |
| U-11 start 直前の手動起動の隙間 | `start` の直前の最終確認と `systemctl start` の間に残る、手動起動の隙間 (§4.2) | **解消 (v0.4): 受容で確定**。帰結は journal の失敗行と再試行だけで、設定・DB は壊れない (§4.2) |
| U-12 リセットの linger | リセットの linger の扱い | **解消 (2026-10-12 ユーザー裁定)**: ウィザードが確認つきで `loginctl enable-linger` を実行し、所有記録を確認記録に durable に残す。リセットは所有記録があり確認を通ったときだけ disable する。手動で有効化された linger は `--include-linger` でも変更しない。記録は linger の処理後に消す (§3 段 13、§4.5、§6.4) |

---

## 付録 A. B-2 実測の要約 (`tmp/design-first-run/b2-measure/`)

- 方法: example の写し (169 行、コメントを含む行 94、空行 20) に 14 項目 (トップレベル・flow list・flow mapping の中・引用符つき・example に無い `datafeed.primary` を含む) を (i) 行単位 (依存追加なし) と (ii) `ruamel.yaml` 0.19.1 の round-trip で当てた。スクリプト `measure.py` `measure2.py`、生ログ `run1.log` `run2.log`、出力 `out_*.yaml` と `out_*.diff`。
- 結果:
  - (i) 13/14 適用 (`datafeed.primary` は「キーが無い」で失敗)。diff +12 −12 (flow mapping の 1 行に 2 項目)。コメント 94→94、空行 20→20、順序同一、意味的一致、現行 loader の検証 OK。
  - (ii) 14/14 適用 (挿入あり)。diff +13 −12。コメント・空行・順序は保存。**ただし無編集の往復でも 3 行変わる** (flow mapping の桁揃えの空白を詰める)。実 settings.yaml (121 行、メモリ上のみ) でも無編集で +3 −3。
  - 実 settings.yaml への 13 項目: (i) 13/13・+8 −8、(ii) 13/13・+9 −9。どちらも意味的一致・検証 OK。
  - 利用者が `pairs` を block list に書き換えた形: (i) は「入れ子で値を持たない」で大声で失敗、(ii) は対応。
  - 複数行スカラー (`improve_at: >-`): (i) は**黙って壊した** (`'Sun 04:00 Sat 03:00'`)。事後の意味的照合 (G2) だけが検出した。(ii) は対応。
  - 存在しないキー: 両方とも失敗として検出。
  - 重複キー: 両方とも検出。**現行の pyyaml loader は黙って後勝ち** (UTC / Sun 04:00)。
  - 注: `load_settings` で検証すると `load_dotenv()` により実 `.env` が測定プロセスの環境に載った (表示・保存なし)。ウィザードの検証は `Settings.model_validate(yaml.safe_load(...))` を使う。
- **推奨: (i) 行単位 + G1 (事前の重複キー検査)・G2 (事後の意味的照合)・G3 (型検証) を必須**。
  - 根拠 1: 触らない行がバイト一致する。(ii) は書き換えゼロでも利用者のファイルを整形し直すので、「要約画面で見せた差分 = 実際の差分」を保証できない。
  - 根拠 2: (ii) だけが扱える形 (block list・複数行・キー挿入) は、ウィザードが書くキーでは避けられる。example はウィザードが書く全キーを 1 行の形で持ち、`primary` は書かずに済む (enabled 1 個から導出されることを実測)。利用者が手で別の形に変えた項目は「手で編集してください」で止まればよい。
  - 根拠 3: (i) の弱点 (黙った壊れ) は G2 が必ず止める (実測で確認)。G2 はどちらの方式でも要る。
  - 根拠 4: 依存を増やさない (公開リポジトリ)。
  - (ii) を選ぶべき条件: ウィザードが書くキーに block list や複数行の値が入るようになったとき。
  - 未実測: 「親 block の末尾に 1 行足す」挿入 (T1 で pin)。これは migration の新規キー挿入にだけ使い、回答の書き込みには使わない (§4.4)。

---

## 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-25 | v1.9 | ウィザード起動 argv と案内文、段 12 の managed 方針、`afx ops <feature> enable` の現行形を追補 | CLI surface の現行運用名を設計資料へ同期するため | |
| 2026-10-02 | v0.1 | 草稿。流れを「集める層 → 確定 1 → 確定 2」に組み直し、B-2 (行単位 + G1〜G3)、B-7 の 3 分岐と未接続時の現物の振る舞い、B-8 ((b): `expected=0` の期の消費印)、unit の差し替え点と lock 解放の順序、未完の手順の一覧、不変条件 15・受入条件 21、task 8 本、未決 8 件、現物の問題 13 件を記述 | 裁定記録 (2026-10-02) と inventory を設計に落とすため | |
| 2026-10-12 | v0.2 | 常駐化を direct exec の user unit に差し替え (screen・対話シェル・start/stop スクリプトを廃止、B-5 は daemon に確定、`afx ctl` を操作の入口に)。段 13 に外から見る手段の一問を足し、bot / web / notify は既存の `afx ops enable <feature>` に任せる (順序: unit 設置 → lock 解放 → enable → 起動)。段 8 を操作 API と `.env` に差し替え (`api.enabled`)。段 4 の疎通確認を非隔離の `verify-backend` と起動時 admission の行に分けた。秘密は Discord token・webhook URL を `afx ops enable` の secret に、ウィザード本体は書かない。承認の導線を bot の二段操作と `afx ctl --as approver approve` に。§5 §6 §7 §8 §9 §10 を更新 (不変条件 IV-16〜18、受入条件 AC-22〜26、問題 P16〜P18・P-14〜P-16、U-4・U-5 解消)。裁定 4 件を反映 (段 8 の操作 API は既定有効で example の既定変更を T8 に、通知は ops の notify に一本化し旧簡易通知の廃止は別チケット、実機受入は別マシンまたは WSL、`afx ops enable` は同一 process の `interactive_enable` 呼び出し。P-14・P-16 解消) | 常駐化 (本番 unit の direct exec 移行) と afx-ops の対話層 (`afx ops enable`) の main 投入、改善 CLI 隔離の有効化 | |
| 2026-10-12 | v0.3 | 設計レビューの指摘を全て反映。(1) 確定 1 を「全部か無か」から回復可能な commit protocol に (durable marker `setup_in_progress`・終了コード 76・fsync つきの置換・DB は 1 transaction・crash matrix、§4.3)。(2) 排他を `setup.lock → instance.lock` に統一し、`afx init` は setup.lock を保持、引き渡し (marker → instance.lock 解放 → enable → marker 除去) と競合試験 (§4.2)。T9 側の変更を task に (lease 引数・`EnableResult`)。(3) `backtest.eval_source: none` を追加、MT5 を使わない分岐は必ず `none` (U-3・P-7 解消)。(4) 起動後確認を 3 段階に (サービス / 認証つき `afx ctl status` / ops ready)、API 無効時は `afx ctl` に出せない旨を明記。(5) settings に `schema_version` と版ごとの migration (§4.4)。(6) ウィザードの IO と ops の IO を別型に、秘密の口は T9 のみ。(7) status 応答の `setup.pending_steps` / `record_state` の固定 schema と前方互換 (§7.3)、ops-api spec の改訂を task に。(8) admission 行を InvocationID / cursor で今回の起動分に絞り、journal 不可・timeout・形式不正を別 reason に。(9) 実機受入の表 (§8.4) と fake TTY の範囲 (isatty / EOF / KeyboardInterrupt)。(10) 範囲外に基本ニュースソース seed のみ・strategy の自動採用なし・Pine Script 対象外を明記。(11) AC-1 を分岐 1 / 2 / 3 の別シナリオに。ユーザー裁定 (2026-10-12) 2 件: 実機受入は別マシンで repo の案内だけで clean clone から立ち上げる (手順書 `docs/operations/first-run-acceptance.md` を成果物に)、やり直し用に `afx init --reset` を追加 (§4.5。§5 に「リセットで消すもの」列、AC-36〜40、T14・T15)。不変条件 IV-19〜26、受入条件 AC-1a〜c・AC-27〜40、task T10〜T15、未決 U-9〜U-12 を追加 | 設計レビューの差し戻し と実機受入の方針裁定 | |
| 2026-10-12 | v0.4 | 設計の再点検の指摘を全て反映し、ユーザー裁定 3 件を入れた。(1) サービスの起動ガードを `run_service` の root 解決の直後 (`ensure_initialized`・`.env`・settings 読み込みより前) の 1 箇所に定義: 75 → 76 の順、marker の内容を読まず存在だけ (`ENOENT` 以外は fail closed)、`build_app` の lock 取得前後の再確認を残す、壊れた・新旧混在の settings でも 76 (§4.3、AC-27、IV-19)。(2) リセットの所有範囲: 操作 API の鍵は `api/<instance_id>/` の当 instance の分だけ、ops の feature は `ops.toml` の `instance_root` が当 root と一致するものだけ、unit は `WorkingDirectory=` が当 root と一致するものだけ。他は「触らない」と表示して除外、対象表示に取引履歴等の永久削除を明記 (§4.5、§5、AC-37・40、IV-26)。(3) 段 4 と受入手順書を local LLM 無しの主シナリオに: llama-swap が応答しなければ local を既定にせず明示選択、Node・Claude CLI・ログイン・`claude -p` の確認、Codex 分岐、別ホストの llama-swap、未導入・未ログインでも `bin_not_found` / `credentials_missing` で完了 (§3 段 4、§8.4、AC-41)。(4) settings schema を変える変更の完了条件 (version bump・migration・旧 fixture・byte 一致 test・理由。§4.4、T1・T8)。(5) journal の観測契約 (InvocationID と `_SYSTEMD_INVOCATION_ID` / `USER_INVOCATION_ID`、cursor の fallback。§6.4、AC-42、T7b の着手前提)。(6) linger: ウィザードが確認つきで有効化し所有記録を確認記録に残す、リセットは所有記録がある場合だけ解除し手動の linger は変更しない (§3 段 13、§4.5、§6.4、§7、IV-26、AC-39、U-12 解消)。(7) task 分割: T5 → T5a・T5b、T7 → T7a・T7b、T15 → T15a・T15b (§9)。(8) AC-36 の許可リストを repo 相対 path に固定。U-9 は実装・移行の受入条件に、U-10・U-11 は本文の扱いで確定。ユーザー裁定 (2026-10-12): リセットは独立スクリプト `scripts/first-run-reset.sh` を主にし、内蔵の `afx init --reset` は後回し (§10.3)、サブマシンには local LLM と MT5 が無いので本環境の bridge と llama-swap を SSH トンネルで見せる (受入手順書の「試験環境の都合」節、利用者向け本文には入れない)、1 回目の受入は人の手で、2 回目以降のリセット・再実行・診断収集は SSH 経由で自動化 (§8.4) | 設計の再点検の差し戻しと実機受入方針の裁定 | |
| 2026-10-12 | v0.5 | 設計レビュー 3 周目の指摘を全て反映し、指揮者裁定 3 件を本文に入れた。(1) `afx check` を fatal (起動不能) と warning (LLM 未検証: `deferred_by_user`・未確認モデル・CLI 未導入 / 未認証・`user_declined_probe`) に分け、warning だけなら unit を設置して degraded で起動する (§3 段 4、§6.2、§6.4、AC-16・16a・41・43、T1・T2・T7a)。Claude 選択時はウィザードが消費確認を出し、同意なら trade 用 `claude -p` を 1 往復、同意しなければ `unverified/user_declined_probe` (AC-41a)。(2) リセットの所有判定の実装契約: bash は orchestration に限り、root の解決と `instance_id`・`setup.json` の JSON 読取り・`ops.toml` の canonical な `instance_root` 行の一致判定だけを `python3 -I` に任せる。API 鍵の base は常に `$HOME/.config`、ops の base は `${XDG_CONFIG_HOME:-$HOME/.config}`、安全に扱えない文字を含む root は計画前に拒否 (§4.5、AC-37、T15a)。(3) 自動化 driver のリセットを `--yes --stop-running --include-linger` に固定し、AC-39 に「1 回目所有 → 非端末 reset → 2 回目も同じ linger 状態」を追加 (§8.4)。(4) §9 の各 task に「所有する受入条件」を追加、共有ファイル (`entry.py`・`service.py`) は関数単位で責務を分け、T14 の所有ファイルに driver と診断収集の実ファイルを明記。(5) AC-36 を製品契約 (AC-36) と試験の clean 判定 (AC-36a: wizard 前の `git status --porcelain=v1 -z --ignored=matching` を baseline とする完全比較) に分離。(6) §10 を仕分け (ユーザー裁定が残る U-1・U-2・U-6 / 実測後 U-7 / 技術調査後 U-8 / 確定済みは 10.4 決定記録へ)、番号と併記の名前を付与。(7) §9 に実装順序案を追加 | 設計レビュー 3 周目と指揮者裁定 | |
| 2026-10-12 | v1.0 | ユーザー承認。U-1 / U-2 / U-6 を推奨どおり確定し決定記録へ。docs/superpowers/specs に移す | 設計レビュー 3 周 (sol → terra → sol) 完了 | (本 commit) |
| 2026-10-13 | v1.1 | §7.3 の固定語彙に `api_disabled` を追加 (§7.2 の「操作 API が無効」行に対応する code が列挙に無かった。status には出さない属性はそのまま) | 実装レビューで §7.2 と §7.3 の不整合を検出 | (本 commit) |
| 2026-10-13 | v1.2 | §7.1 の確認記録 `unit` に `placed_not_reloaded` を追加 (unit は置いたが `daemon-reload` に失敗した状態を、`installed` とも `not_installed` とも区別して残す)。`afx check` の CLI `--version` 失敗は `version_check_failed` の warning とし起動を拒否しない | 実装レビューの指摘 | |
| 2026-10-13 | v1.3 | §7.1 の `isolation` は必須キーのまま、改善 backend が CLI でないとき (local) は `{admitted: unknown, reason_code: not_cli_backend}` を記録する。reason の語彙に `not_cli_backend` を追加 | 実装レビューの指摘 | |
| 2026-10-13 | v1.4 | 確認記録 `linger` に `not_checked` (確定 1 の時点の値) を追加し、`linger_off` は `declined` / `failed` のときだけ出す。marker の内容形式を固定 (`phase` / `started_at` / `settings_sha256` の `key=value` 行、handoff は sha 無し)。モデル一覧の取得を `setup/checks.py::list_llama_swap_models` に置く | ウィザード本体の実装で、段 10 の時点に書ける linger の値と marker の読み方が未定義だった | |
| 2026-10-13 | v1.5 | §3 段 4: backend を選んだ後にモデルを選ぶ (claude は alias 固定表で既定 `sonnet`、codex は `codex app-server` の `model/list` で既定 `terra`、opencode は `opencode models` の一覧から必ず選ぶ、10 件超は絞り込み 1 問、一覧失敗は固定 reason `model_list_unavailable` で手入力)。runner は settings の `model` をそのまま `--model` に渡し既定を持たないため | ユーザー裁定 (2026-10-13) | (本 commit) |
| 2026-10-14 | v1.6 | §4.5 step 5 の取り除く順序を §5 の「リセット」列と実装に揃える (config → unit (primary は最後、補助 unit の失敗時は残す) → repo → linger → `data/` → `.env`。`daemon-reload` / `reset-failed` は primary 所有時のみ、`reset-failed` の非 0 許容)。段 11 の標準指標は T6 の `run_indicator_stage` を既定で呼ぶ | T15b の閉鎖確認で §4.5 と §5 の順序が食い違っていたため。T5a と T6 の配線 | |
| 2026-10-15 | v1.7 | 受入 2 回目 (stream-svr) の是正: 段 0 で改善隔離の Landlock ABI 8 要件を判定して先に言い、段 4 の実往復と段 7 に反映、記録 reason と未完手順 `improve_isolation_unavailable`。段 4 は claude の実体の絶対パスを `runner.claude.bin` に書く (裸の名前は mission 側で cwd 相対に解決しない)。§4.5 の repo 内削除は `chmod -R u+rwX` を先に行う。AC-36a の許可集合に `**/__pycache__/` と `tmp/` | チケット wizard-check-improve-landlock-abi-upfront / wizard-write-absolute-cli-bin / reset-cannot-remove-readonly-plugin-versions (いずれも実機で再現) | |
| 2026-10-15 | v1.8 | 受入 1〜2 回目のウィザード対話の見直し: 段 3 は平文 http の鍵つき bridge を `key_withheld_plain_http` で unverified のまま進める (裁定 (b))、段 5 は 15m/30m/1h の番号選択 (選べない足は理由つき)、段 8 は「常駐サービスで動かすか」を問い API と unit をその結果に、段 9 は利用者の言葉の要約と `d` の差分、段 11 は 1 問 (再実行は 1 行)、段 12 は用途・例文・`e`、段 13 は再質問なし・linger 同文の重複なし・`uv run afx`・`user_session_missing`。未完手順の `.env` 判定は `.env` ファイルも見る、`bridge_not_connected` は data_state=ready で出さない | 受入のユーザー指摘 (チケット wizard-* 8 件 + pending-env-key-check-reads-process-env + unit-placement-explain-missing-user-session) | |
