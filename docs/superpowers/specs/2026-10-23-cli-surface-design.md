# afx / afx ctl コマンド面整理設計書

**版:** v0.2 (設計レビュー 1 周目反映)

**対象:** `cli-surface-principle-and-reorg`

**前提:** 2026-10-17 のユーザー裁定で確定した「7 用途群 × 2 実行レーン」を実装可能な契約へ落とす。

**実装単位:** メタデータ schema → 束 B (backlog / policy API と parse) → 束 A (help、ローカル CLI、文書) の順で作る。A + B は 1 release とし、実装済み parser / endpoint との統合 task が完了するまで新しい record は help へ公開しない。

## 0. 目的・範囲・決定

利用者が「何をしたいか」と「daemon が必要か」を最初の help だけで判断できるようにする。現在は最上位 `afx`、`afx ctl`、daemon 内の対話 shell が別々の文字列を持つ。新しい操作を足すたびに help、認可、文書がずれる構造を解消する。

| 項目 | 決定 |
|---|---|
| 分類 | 7 用途群を固定する。群は導入・接続管理 / 稼働観測・実行追跡 / 市場入力を整える / 調査・評価する / 問い合わせ・改善運用 / Plugin を採否・配備する / 保護状態を回復する、の順とする。 |
| レーン | 最上位 `afx` は daemon を介さない操作とする。`afx ctl` は稼働 daemon の認証・認可・監査つき API とする。`ctl` 自体は群に分類しない。 |
| 危険度 | 群名へ混ぜない。各操作へ `[読取]`、`[変更]`、`[確認]`、`[approver]` を付ける。API 操作は scope と確認要否から照合する。 |
| 正所属 | 1 操作は 1 群、1 レーンにだけ載せる。横断表示は出力末尾の「次の一手」で別の操作へ誘導する。 |
| help の正本 | `src/agentic_fx/ops/surface.py` に純データの分類メタデータを置く。`afx --help`、`afx ctl help`、対話 shell の `_HELP` をそこから作る。 |
| `verify-backend` | 正式名を `afx check backend --backend <name> [--provider chatgpt]` にする。`check` の引数なし形は従来どおりローカルの読み取り専用検査とする。`backend` という明示サブコマンドが実往復を示すため `--online` は足さない。 |
| backlog | 正式名を `afx ctl backlog add <idea>` にする。`improve add` は互換別名として残す。`backlog list [--all] [--limit N]` を足す。 |
| policy | `afx ctl policy show` と `afx ctl policy remove <id>` を足す。`<id>` は安定した `ops_policies.id` とする。 |
| plugin | `afx plugin list [--kind indicator\|strategy]` を足す。indicator と strategy を別の用途群へ分けない。 |
| offline status | `afx status` を足す。daemon 稼働中は詳細を直接読まず `afx ctl status` へ誘導する。daemon 停止中だけローカル記録を fail-soft に読む。 |
| API の非目標 | 発注、SL 変更、ポジションのクローズ、サービス停止の API は作らない。 |

### 0.1 範囲外

- ウィザード対話の見栄えは別束である。
- `afx news` の本体は `news-source-cli-and-429-backoff` で設計する。本書では群の planned metadata だけを定義し、実装まで help には出さない。
- 発注系 API は追加しない。
- `afx-ops <bot|web|notify>` は unit の入口であり、利用者向け操作面ではない。
- daemon や売買ロジックの動作は変えない。

## 1. 用語

| 用語 | 意味 |
|---|---|
| 群 | 利用者の目的による分類である。実装モジュール、動詞、危険度では分類しない。 |
| レーン | 同じ目的へ到達する実行境界である。左レーンは最上位 `afx`、右レーンは `afx ctl` である。 |
| ラベル | 操作の副作用と追加確認を示す表示である。`[読取]` は状態を変えない。`[変更]` は状態を変える。`[確認]` は端末確認を要する。`[approver]` は approver principal を要する。 |
| scope | API の認可単位である。表示ラベルとは別の機械契約である。本束で自由文 guidance の読取専用 `guidance.read` を追加し、operator / approver だけに与える。他に `status.read`、`logs.read`、`operate`、`decide`、`local_guard` を使う。 |
| 正所属 | help の分類表へ 1 回だけ載せる所属である。たとえば `afx ctl status` は複数状態を表示しても「稼働観測・実行追跡」だけに置く。 |
| 誘導 | 正所属以外の関心へ、説明または出力末尾の固定文から案内することである。help 上の重複掲載はしない。 |
| 正式名 | 新しい文書と help が示す唯一のコマンド名である。 |
| 互換別名 | 正式名と同じ `Op`、引数、認可、確認、終了コードへ正規化される旧名である。 |
| 廃止名 | 1 リリースだけ構文を受理し、固定の移行案内を stderr に出して rc=2 で終了する名前である。副作用は起こさない。 |

`[変更]` は必ずしも危険を意味しない。たとえば backtest の記録作成も `[変更]` である。`[確認]` と `[approver]` は独立である。`jobs cancel` は `[確認]` だが `[approver]` ではない (`ops/client.py:879-881`)。

## 2. 分類表

### 2.1 完成形

表の「scope」は API レーンだけに値を持つ。最上位レーンは `—` とする。「要 daemon」は実行時に稼働 daemon が必要かを示す。「停止中のみ」と「再起動後反映」は追加条件であり、群名やラベルにはしない。

| 群 | レーン | コマンド | ラベル | 認可 scope | 要 daemon | 定義場所 |
|---|---|---|---|---|---|---|
| 導入・接続管理 | `afx` | `afx init [--non-interactive] [--config FILE]` | `[変更]` | — | いいえ | `entry.py:34-37,63-73` |
| 導入・接続管理 | `afx` | `afx check [--json] [--unit-env]` | `[読取]` | — | いいえ | `entry.py:38-43,74-76` |
| 導入・接続管理 | `afx` | `afx check backend --backend <local\|claude\|codex\|opencode> [--provider chatgpt]` | `[変更]` | — | いいえ | `backtest/cli.py:191-202,792-811` を移設 |
| 導入・接続管理 | `afx` | `afx improve verify-backend …` **(廃止名、1 リリースのみ)** | `[変更]` | — | いいえ。実処理せず案内だけ | `backtest/cli.py:191-202,868-869` を compatibility shim 化 |
| 導入・接続管理 | `afx` | `afx keys init` | `[変更]` | — | いいえ。サービス停止中のみ | `entry.py:44-46` |
| 導入・接続管理 | `afx` | `afx keys enroll <principal>` | `[変更]` | — | いいえ。サービス停止中のみ | `entry.py:47-51` |
| 導入・接続管理 | `afx` | `afx keys rotate <principal>` | `[変更]` | — | いいえ。サービス停止中のみ | `entry.py:47-51` |
| 導入・接続管理 | `afx` | `afx keys revoke <principal>` | `[変更]` | — | いいえ。サービス停止中のみ | `entry.py:47-51` |
| 導入・接続管理 | `afx` | `afx ops enable <bot\|web\|notify>` | `[変更]` | — | いいえ。必要時だけ daemon を一時停止し、元が稼働中なら再開 | `entry.py:52-59,80-85`; `ops_setup/stages.py` |
| 導入・接続管理 | `afx` | `afx ops disable <bot\|web\|notify>` | `[変更]` | — | いいえ | `entry.py:52-59,80-85` |
| 導入・接続管理 | `afx` | `afx ops status <bot\|web\|notify>` | `[読取]` | — | いいえ | `entry.py:52-59,80-85` |
| 導入・接続管理 | `afx ctl` | `afx ctl whoami` | `[読取]` | `status.read` | はい | `client.py:902,1021`; `service.py:82` |
| 稼働観測・実行追跡 | `afx` | `afx status` | `[読取]` | — | いいえ | 新設 `status.py`; `entry.py` |
| 稼働観測・実行追跡 | `afx ctl` | `afx ctl status` | `[読取]` | `status.read` | はい | `client.py:902,1017`; `service.py:75` |
| 稼働観測・実行追跡 | `afx ctl` | `afx ctl log [n]` | `[読取]` | `logs.read` | はい | `client.py:904,1028`; `service.py:76` |
| 稼働観測・実行追跡 | `afx ctl` | `afx ctl activity [n] [category]` | `[読取]` | `logs.read` | はい | `client.py:906,1032`; `service.py:77` |
| 稼働観測・実行追跡 | `afx ctl` | `afx ctl events [--after ID] [--limit N] [--follow]` | `[読取]` | `events.read` | はい | `client.py:911,956,1052`; `service.py:84` |
| 稼働観測・実行追跡 | `afx ctl` | `afx ctl jobs get <id>` | `[読取]` | `jobs.own` | はい | `client.py:949,1048`; `service.py:83` |
| 稼働観測・実行追跡 | `afx ctl` | `afx ctl jobs cancel <id>` | `[変更] [確認]` | `jobs.own` | はい | `client.py:949,1305`; `service.py:110` |
| 市場入力を整える | `afx` | `afx news …` | `[読取] / [変更]` は下位操作ごと | — | いいえ | 後続 `news-source-cli-and-429-backoff`。本書は予約行のみ |
| 市場入力を整える | `afx` | `afx history import …` | `[変更]` | — | いいえ | `backtest/cli.py:68-78` |
| 市場入力を整える | `afx` | `afx history compare …` | `[読取]` | — | いいえ | `backtest/cli.py:80-84` |
| 市場入力を整える | `afx` | `afx history coverage …` | `[読取]` | — | いいえ | `backtest/cli.py:86-92` |
| 調査・評価する | `afx` | `afx backtest run …` | `[変更]` | — | いいえ | `backtest/cli.py:94-112` |
| 調査・評価する | `afx` | `afx analyze corr …` | `[変更]` | — | いいえ | `backtest/cli.py:114-125` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl ask <question>` | `[変更]` | `operate` | はい | `client.py:928,1095`; `service.py:85` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl improve` | `[変更]` | `operate` | はい | `client.py:930,1104`; `service.py:98-99` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl backlog add <idea>` | `[変更]` | `operate` | はい | `client.py:932-933,1113`; `service.py:100-101` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl improve add <idea>` **(互換別名)** | `[変更]` | `operate` | はい | `client.py:932-933` を canonical `backlog_add` へ正規化 |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl backlog list [--all] [--limit N]` | `[読取]` | `guidance.read` | はい | 新設 `client.py`; `service.py`; `store/backlog.py` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl backlog reject <id>` | `[変更]` | `operate` | はい | `client.py:934,1126`; `service.py:102-103` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl backlog reopen <id>` | `[変更]` | `operate` | はい | `client.py:934,1126`; `service.py:104-105` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl backlog note <id>` | `[変更]` | `operate` | はい | `client.py:934,1126`; `service.py:106-107` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl policy add <text>` | `[変更]` | `operate` | はい | `client.py:936,1142`; `service.py:108-109,699-718` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl policy show` | `[読取]` | `guidance.read` | はい | 新設 `client.py`; `service.py` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl policy remove <id>` | `[変更]` | `operate` | はい | 新設 `client.py`; `service.py` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl reflect retry <order_id>` | `[変更]` | `operate` | はい | `client.py:938,1149`; `service.py:81,96-97` |
| Plugin を採否・配備する | `afx` | `afx plugin submit <name> --from _human` | `[変更]` | — | いいえ | `backtest/cli.py:129-135` |
| Plugin を採否・配備する | `afx` | `afx plugin bless <name> --from _human` | `[変更]` | — | いいえ | `backtest/cli.py:136-141` |
| Plugin を採否・配備する | `afx` | `afx plugin materialize <name>` | `[変更]` | — | いいえ | `backtest/cli.py:142-145` |
| Plugin を採否・配備する | `afx` | `afx plugin lock <name> --from _human` | `[変更]` | — | いいえ | `backtest/cli.py:146-152` |
| Plugin を採否・配備する | `afx` | `afx plugin retire <name> …` | `[変更] [確認]` | — | いいえ。運用時間外 | `backtest/cli.py:153-159` |
| Plugin を採否・配備する | `afx` | `afx plugin disable <name> …` | `[変更] [確認]` | — | いいえ。再起動後反映 | `backtest/cli.py:160-168` |
| Plugin を採否・配備する | `afx` | `afx plugin rollback <name> …` | `[変更] [確認]` | — | いいえ。再起動後反映 | `backtest/cli.py:169-179` |
| Plugin を採否・配備する | `afx` | `afx plugin revoke <name> --version HASH …` | `[変更] [確認]` | — | いいえ | `backtest/cli.py:180-186` |
| Plugin を採否・配備する | `afx` | `afx plugin versions <name>` | `[読取]` | — | いいえ | `backtest/cli.py:187-190,820-830` |
| Plugin を採否・配備する | `afx` | `afx plugin list [--kind indicator\|strategy]` | `[読取]` | — | いいえ | 新設 `backtest/cli.py`; `plugin_deploy_cli.py` |
| Plugin を採否・配備する | `afx ctl` | `afx ctl approval list [n]` | `[読取]` | `approvals.list` | はい | `client.py:913-916,1039`; `service.py:80` |
| Plugin を採否・配備する | `afx ctl` | `afx ctl approval show <id>` | `[読取]` | `approvals.detail` | はい | `client.py:913-921,1044`; `service.py:78-79` |
| Plugin を採否・配備する | `afx ctl` | `afx ctl approve <id>` | `[変更] [確認] [approver]` | `decide` | はい | `client.py:922,1183`; `service.py:88-89` |
| Plugin を採否・配備する | `afx ctl` | `afx ctl reject <id> [reason]` | `[変更] [確認] [approver]` | `decide` | はい | `client.py:926,1202`; `service.py:90` |
| Plugin を採否・配備する | `afx ctl` | `afx ctl approval retry <id>` | `[変更] [確認] [approver]` | `decide` | はい | `client.py:917-918,924,1194`; `service.py:91-92` |
| 保護状態を回復する | `afx ctl` | `afx ctl killswitch reset` | `[読取]` | `status.read` | はい | `client.py:940-944,1171`。世代なしは表示 |
| 保護状態を回復する | `afx ctl` | `afx ctl killswitch reset <generation>` | `[変更] [確認] [approver]` | `local_guard` | はい | `client.py:940-944,1242`; `service.py:93-94` |
| 保護状態を回復する | `afx ctl` | `afx ctl killswitch reconcile [confirm]` | `[変更] [確認] [approver]` | `local_guard` | はい | `client.py:945,1268`; `service.py:95` |
| 保護状態を回復する | `afx ctl` | `afx ctl data resume [--acknowledge]` | `[変更] [確認] [approver]` | `local_guard` | はい | `client.py:947,1291`; `service.py:86-87` |

現行 24 + 24 の数え方は survey §1 を踏襲する。`killswitch reset` は表示形と変更形を別の完成形として表へ出す。`help` は表示機能、`stop` は拒否用の罠、素の `afx` / `afx --daemon` はサービス入口なので分類対象へ数えない。

最上位の `improve` 群は廃止する。上表の `afx improve verify-backend` は分類上の正式所属ではなく、1 リリースだけ存在する廃止名の記録である。通常 help の左列には `improve` を出さない。

### 2.2 別名と廃止名

| 入力 | 扱い | 固定文言 | 終了コード |
|---|---|---|---|
| `afx improve verify-backend …` | 1 リリースだけ残す廃止名。新処理を呼ばない | ``afx improve verify-backend` は廃止されました。`afx check backend --backend <name> [--provider chatgpt]` を使ってください。` | 2 |
| `afx ctl improve add <idea>` | `backlog add` の互換別名。正式名と同じ `Op("backlog_add")` へ正規化する | ``afx ctl improve add` は旧名です。`afx ctl backlog add` を使ってください。` | 正式名と同じ。案内は stderr |
| `approvals list` / `approvals show` | 既存互換別名として維持 | なし | 正式名と同じ |
| `approval <id>` | `approval show <id>` の既存短縮として維持 | なし | 正式名と同じ |
| `retry <id>` | `approval retry <id>` の既存短縮として維持 | なし | 正式名と同じ |

廃止名は `plugin bless` / `submit` の旧経路が固定案内で拒否する流儀に合わせる (`backtest/cli.py:567-597`)。`afx improve verify-backend` は argparse へ `improve` parser を登録せず、`entry.main()` の parser 構築・`parse_args()` より前の argv shim だけで完全一致の語順を捕捉する。このため positional choices、`afx --help`、分類 metadata の通常 record のいずれにも `improve` は現れない。

廃止 compatibility record は `deprecated_since="0.2.0"`、`remove_in="1.0.0"` を持つ。実行中 package version が `remove_in` 以上なら record が残っている test を失敗させ、期限超過の shim を自動検出する。互換別名は処理を継続するため別の API endpoint を作らない。別名そのものは分類表と通常 help に重複掲載せず、「互換名」節だけに載せる。

### 2.3 対話 shell

対話 shell はこの版で廃止しない。daemon 内から `ShellCaller.SHELL` として同じ ops 関数を呼ぶ既存の互換 UI と位置づける (`ops/contracts.py:76-83`)。第 3 のレーンとは呼ばない。本 release で deprecated 表示を help 末尾の 1 行に追加し、次 major (`1.0.0`) で削除する案を既定とする。最終裁定は §8.2-1 に残す。

`commands.py:28-47` の手書き `_HELP` は削除する。分類メタデータのうち `shell=True` の操作から help を生成する。shell に実装されていない `events`、`jobs`、`whoami` は生成対象にしない。`improve add` は `backlog add` を正式表示にし、旧形を互換別名として受理する。

`stop` は `classified=False, shell=True, label="change"` の互換 record として同じ metadata module に置く。7 群の表や `afx ctl help` には出さないが、shell help には `stop  daemon を graceful shutdown する` と表示する。dispatch は現行の shutdown 要求経路を維持し、API の「サービス停止は無い」と混同させない。help 末尾に `廃止予定: 対話 shell は 0.2.0 で deprecated。1.0.0 で削除予定。afx ctl を使用してください。` の 1 行を出す。

## 3. 分類メタデータと help 生成

### 3.1 正本と依存方向

新規 `src/agentic_fx/ops/surface.py` は純データ module とする。import は stdlib と `agentic_fx.ops.contracts` だけに限る。`.env`、setup 永続層、service、DB、httpx を import しない。

この制約により `ops/client.py` が setup 永続層を load しない既存 pin を維持する (`tests/ops/test_ctl_pins.py:639`)。`client.py` から `service.py` を import してはならない。認可表との照合は production import ではなく test で行う。

各 command record は次を持つ。

| field | 内容 |
|---|---|
| `group`, `group_order` | 7 群の enum と固定順 |
| `lane` | `local` / `ctl` |
| `command`, `usage` | 正式名と引数を含む表示文字列 |
| `label` | `read` / `change`。`confirm` と `approver` は独立 bool |
| `scope` | `Scope` または `None` |
| `endpoint_code` | `ENDPOINTS` と照合する code または `None` |
| `daemon_required` | bool |
| `condition` | `停止中のみ`、`再起動後反映` 等の短い注記 |
| `summary` | 1 文の説明 |
| `definition` | file:line の保守用根拠 |
| `aliases` | parser が受ける互換名。通常 help には展開しない |
| `shell` | 対話 shell に存在するか |
| `classified` | 7 群の分類表に載せるか。shell 専用 `stop` は false |
| `implemented`, `exposed` | parser / endpoint 実装済みか、help 公開済みか。`exposed=True` は `implemented=True` を必須とする |
| `planned` | `news` のように別束で実装する予約行か。`planned=True` は通常 help から必ず除外 |
| `deprecated_since`, `remove_in` | 廃止 compatibility record の package version。通常 record は `None` |

`entry.py` は argparse の parser 群を従来どおり登録する。分類メタデータは parser 実装を置き換えない。表から `parser.epilog` を組み立て、`RawDescriptionHelpFormatter` で改行を固定する。`client.py` の静的 `USAGE` は `render_ctl_help()` の戻り値へ置き換える。parser とメタデータの全件一致を test で保証する。

### 3.2 `afx --help` の全文 golden

formatter の幅を 100 に固定し、stdout 全体を次の byte 列と一致させる。左レーンの親 command にもラベルを付け、副作用が下位操作ごとに異なる場合は `[下位操作ごと]` とする。各 `afx <sub> --help` では葉操作ごとに `[読取]` / `[変更]` / `[確認]` と condition を出す。

```text
usage: afx [-h] [--daemon] {init,check,keys,ops,ctl,status,history,backtest,analyze,plugin} ...

agentic-fx

positional arguments:
  {init,check,keys,ops,ctl,status,history,backtest,analyze,plugin}
    init                [変更] 初期設定 (対話。`--non-interactive` で従来の非対話)
    check               [下位操作ごと] 設定と実行環境の検査 (`backend` は課金枠を使う非隔離の接続確認)
    keys                [変更] 操作 API の鍵管理 (停止中のみ)
    ops                 [下位操作ごと] 外部 operations 機能の有効化・無効化・状態表示
    ctl                 稼働中の daemon を操作 API で操作する (afx ctl help)
    status              [読取] オフライン状態を書き込まずに表示する
    history             [下位操作ごと] 履歴データ操作
    backtest            [変更] バックテスト
    analyze             [変更] 履歴分析
    plugin              [下位操作ごと] plugin の採否・配備・表示

options:
  -h, --help            show this help message and exit
  --daemon              systemd 用 (コマンド受付なし)

実行レーン:
  afx <sub>      daemon を介さない。停止中でも実行できる
  afx ctl <op>   稼働 daemon の認証・監査つき操作 API

用途                         daemon 不要                         稼働 daemon
導入・接続管理               [変更] init / [読取・変更] check / [変更] keys / [読取・変更] ops   [読取] ctl whoami
稼働観測・実行追跡           [読取] status                     ctl status / log / activity / events / jobs
市場入力を整える             [読取・変更] history             —
調査・評価する               [変更] backtest / [変更] analyze    —
問い合わせ・改善運用         —                                   ctl ask / improve / backlog / policy / reflect retry
Plugin を採否・配備する      [読取・変更] plugin              ctl approval / approve / reject
保護状態を回復する           —                                   ctl killswitch / data resume

ラベル: [読取] 状態を変えない  [変更] 状態を変える  [確認] 端末確認  [approver] approver 権限
詳しい操作: afx <sub> --help / afx ctl help
```

`news` は `planned=True, implemented=False, exposed=False` の予約 record とし、本 release の choices と help には出さない。同様に `backlog list` / `policy show` / `policy remove` は B の parser / endpoint と test が完了するまで `exposed=False` とし、最終統合 task でだけ true にする。help は実装済みの利用可能性を表す契約である。

### 3.3 `afx ctl help` の固定出力

```text
usage: afx ctl [--as operator|approver] <操作>
       afx ctl [--as operator|approver]        (端末なら対話モード afx ctl>)

導入・接続管理:
  [読取] whoami

稼働観測・実行追跡:
  [読取] status
  [読取] log [n] / activity [n] [カテゴリ]
  [読取] events [--after <id>] [--limit <n>] [--follow]
  [読取] jobs get <id>
  [変更] [確認] jobs cancel <id>

市場入力を整える:
  —

調査・評価する:
  —

問い合わせ・改善運用:
  [変更] ask <質問> / improve
  [変更] backlog add <idea>
  [読取] backlog list [--all] [--limit <n>]
  [変更] backlog reject|reopen|note <id>
  [変更] policy add <text>
  [読取] policy show
  [変更] policy remove <id>
  [変更] reflect retry <order_id>

Plugin を採否・配備する:
  [読取] approval list [n] / approval show <id>
  [変更] [確認] [approver] approve <id> / reject <id> [理由] / approval retry <id>

保護状態を回復する:
  [読取] killswitch reset
  [変更] [確認] [approver] killswitch reset <世代> / killswitch reconcile [confirm]
  [変更] [確認] [approver] data resume [--acknowledge]

互換名: improve add <idea> / approvals list / approvals show / approval <id> / retry <id>
API に発注・SL 変更・クローズ・サービス停止は無い。
```

末尾の 1 文は固定する。`stop` を操作一覧へ載せない。`afx ctl stop` は従来どおり API へ送らず `systemctl --user stop` の案内と rc=2 を返す (`client.py:104`)。

### 3.4 一致 test

- `surface.py` の正式な local 親 command と argparse の全 `add_parser` を一致させる。予約行 `news` と `ctl` ダミー parser は明示的な例外集合にする。
- ctl の正式名と互換別名を parser table 化するか、独立 fixture から `parse()` へ全語順を与える。各正式名は 1 record にだけ対応させる。
- API command の `endpoint_code`、route、scope、mutating を `ENDPOINTS` と比較する。この 28 route 照合 (AC-4) は B1 / B2 で endpoint と parse を実装した後の最終統合 task で有効化し、schema task 単独の完了条件にはしない。`tests/ops/test_ac01_02_43_api_routing.py:251-288` の `_EXPECTED_TABLE` と同じ独立表の流儀を使う。
- `[読取]` は `ENDPOINTS[code].mutating is False` と一致させる。`[変更]` は `True` と一致させる。
- `[approver]` は `_APPROVER_OPS` と一致させる。`[確認]` は `_CONFIRM_OPS` と一致させる。`jobs cancel` の差を pin する。
- `surface.py` の `scope` は `ENDPOINTS[code].scope` と完全一致させる。
- endpoint は 3 本増えて 25 から 28 になる。`ops/api_server.py:274-281`、`tests/ops/test_ac01_02_43_api_routing.py:91,282` の個数 pin と `_EXPECTED_TABLE` は B1 / B2 完了後の最終統合 task で同時に更新する。
- `approval_change` は CLI の詳細表示が内部で使う endpoint だが、独立コマンドではない。分類漏れとは扱わない。

## 4. レーン規則に合わないものの裁定

### 4.1 `keys` と `ops enable`

どちらも最上位レーンに残すが、停止契約は分ける。`keys` だけは `condition="停止中のみ"` とし、daemon 稼働を検知したら無変更で rc=1 を返す。

`ops enable` は現行の durable 状態機械を保つ。すなわち、marker 作成 → daemon 停止と停止確認 → instance lock 取得 → enroll / install → lock 解放 → 元が稼働中なら daemon 再起動 → marker 除去の順である。metadata の condition は「必要なときだけ一時停止して再開」とし、稼働中を rc=1 で拒否しない。crash 後の再開と元が停止中のときは起動しない既存契約も維持する。これにより §0.1 の「daemon の挙動は変えない」と整合する。

### 4.2 `verify-backend`

`afx check` の引数なし形はローカル読み取り専用のままにする。ネットワーク往復とサブスク枠を使う検査は `afx check backend` という明示サブコマンドへ分離し、`[変更]` と表示する。

`backend` がオンライン検査であることはコマンド名で明示できるため `--online` は足さない。ただし成否は接続性だけを示し、service の seccomp / cgroup に適合することを示さない。実 backend を呼ぶ直前に次の stdout 2 行を byte 一致で出す。

```text
この確認は運用者の shell で、service の追加隔離 (seccomp / cgroup) なしに疎通だけを見る。隔離適合の合格ではない。隔離付きの自己試験の結果は service 起動時の技術ログ (`improve isolation: enabled; … fingerprint=`) を見る
実 backend へ 1 往復し、課金またはサブスク枠を使う。
```

`verify_backend(..., isolation_options=OPERATOR_SHELL_NO_ISOLATION)` と既存 sentinel の利用を契約にし、sentinel を隔離ありの値にすり替える変異を test で殺す。現在の `ensure_initialized` と DB 初期化は不要なので外す (`backtest/cli.py:822,839`)。settings と `.env` は読む。scheduler、wave、backlog、DB は変更しない。隔離適合の確認は §11 の将来の `afx ctl` service job に限る。

### 4.3 `plugin bless`

最上位レーンに残す。これは daemon の approval queue を決定する API 操作ではなく、人間が `_human` 候補をローカルで検証し、即時配備する明示経路である (`backtest/cli.py:136-141`)。

help は「ローカル即時承認。API principal の確認は通らない。plugin の journal / approval / activity 監査は残る」と書く。`afx ctl approve` の代替であるかのように説明しない。`--from _human` なしの旧経路は従来どおり固定案内で拒否する。

### 4.4 `jobs cancel`

`[変更] [確認]` とする。scope は `jobs.own` のままにする。operator が自分の job を取り消す操作であり、approver の決定ではない。`_CONFIRM_OPS` には含め、`_APPROVER_OPS` には含めない (`client.py:879-881`)。

### 4.5 `whoami`

「導入・接続管理」の ctl レーンを正所属とする。認証した接続先と principal を検査する目的だからである。daemon が止まっていると使えない事実はレーン説明で示す。稼働観測へ重複掲載しない。

### 4.6 別名、廃止名、素の `afx`、対話 shell

別名と廃止名は §2.2 の 2 種に分ける。廃止名は parser の通常 command record へ入れず、argv shim 用の期限つき compatibility record にする。`deprecated_since="0.2.0"` と `remove_in="1.0.0"` を record に持たせ、期限 pin で検査する。

素の `afx` と `afx --daemon` はサービス入口なので 7 群へ入れない (`entry.py:92-98`)。この版では起動挙動を変えない。最上位 help の「実行レーン」は subcommand の分類であり、daemon executable 自体の説明ではない。

対話 shell は §2.3 のとおり互換 UI として残す。`_HELP` の重複だけを除く。shell が API 認証を通らない点は隠さず、`ShellCaller.SHELL` の既存監査境界であると運用文書へ書く。

### 4.7 `plugin list` の view model

`plugin list` は live FS、pending approval、deploy journal を独立に読み、どれかが無い、壊れている、または DB table 不足でも他の source を表示する。読取モデルを次で固定する。

```text
PluginListView(
  sources={live: ok|missing|unavailable, pending: ok|missing|unavailable, journal: ok|missing|unavailable},
  items=[{name, kind: indicator|strategy|unknown, live: [version], pending: [version],
          open_ops: ["<id>:<operation>:<target>"], issues: [code]}]
)
```

1 行は `(name, kind)` 1 組とする。`version` は immutable な content hash (表示は一意な最短 12 文字以上の prefix、衝突時は full hash) である。下の `v1` / `v2` は fixture の短い hash 値である。同じ name の indicator と strategy は別行にし、name 昇順、kind は `indicator` → `strategy` → `unknown` の順とする。同じ source 内の重複 name / version は上書きせずすべて保存し、version → approval id / operation id の順に並べ、`issues` に `duplicate_live`、`duplicate_pending` または `duplicate_open_op` を追加する。manifest を読めない plugin も落とさず `kind=unknown`、`issues=[broken:<fixed-reason>]` で出す。`--kind` 指定時は指定 kind の行だけを出し、unknown 行は出さないが source status は常に出す。

stdout は source status 3 行と固定列 `NAME KIND LIVE PENDING OPEN_OP ISSUES` からなる。配列は `,` で結合し、空は `-` とする。live v1 + pending v2 + 未完 rollback を同時に持つ固定例は次とする。

```text
source live: ok
source pending: ok
source journal: ok
NAME       KIND       LIVE  PENDING  OPEN_OP               ISSUES
momentum   strategy   v1    v2       17:rollback:v1         -
rsi        indicator  -     -        -                     broken:invalid_manifest
```

live directory 不在は `source live: missing`、DB 不在は pending / journal の両方を `missing`、table 不足・破損・permission error は該当 source だけ `unavailable` とする。missing は rc=0、broken item または unavailable が 1 つでもあれば残りを表示して rc=1 とする。どの経路も DB、directory、lock file を作らない。

## 5. 新設 API

### 5.1 endpoint

| code | route | scope | mutating | idempotent | autopilot 制限 |
|---|---|---|---|---|---|
| `backlog_list` | `GET /v1/backlog` | `guidance.read` | false | — | — |
| `policy_show` | `GET /v1/policy` | `guidance.read` | false | — | — |
| `policy_remove` | `POST /v1/policy/{id}/remove` | `operate` | true | true | true |

`api_server.py` は現在 GET / POST だけを受ける (`api_server.py:283-286`)。remove も既存の idempotency と監査の枠へ載せるため POST とする。endpoint 総数は 28 になる。

読取は新 scope `guidance.read` とする。backlog の idea / last_result と policy directive は利用者の自由文であり、`logs.read` の流用はしない。`PRINCIPAL_SCOPES` は operator / approver にだけ `guidance.read` を追加し、ops-web / ops-bot / ops-notify には与えない。endpoint ごとの特例 deny ではなく scope 照合で一貫して拒否する。`tests/ops/test_scope_enforcement.py::test_scope_table_matches_the_spec`、`::test_every_endpoint_admits_exactly_the_principals_holding_its_scope`、`tests/ops/test_ac56_57_ops_principals.py::test_ac56_authorization_table_matches_the_spec_for_every_principal`、`::test_ac56_every_route_is_allowed_only_by_the_spec_scope` の principal × scope pin を更新する。bot、web、notify の UI / route も追加しない。

### 5.2 `backlog list`

既定は活動中の `open`、`observation`、`note`、`selected` を返す。`--all` は `done` と `rejected` も加える。並びは `updated_at DESC, id DESC` とする。既定上限は 100、`--limit` の許容範囲は 1〜500 とする。

応答は次の固定 shape とする。

```json
{"items":[{"id":12,"status":"open","source":"user","updated_at":"2026-10-23T00:00:00+00:00","idea":"...","idea_truncated":false,"last_result":null,"last_result_truncated":false}],"limit":100,"has_more":false}
```

API 応答の `idea` と `last_result` は redactor と改行 / 制御文字の正規化後にそれぞれ UTF-8 4,096 byte を上限とする。超過時は文字境界で切り、上限内の末尾を固定印 `…[truncated]` にし、対応する `idea_truncated` / `last_result_truncated` を true にする。`null` は切り詰めず false である。これにより 500 件の最大応答も有界にする。CLI は 1 行 1 件で `#<id> <status> <source> <updated_at> <idea>` を出す。`last_result` があるときだけ次行へ 2 空白 indent で出す。0 件は `backlog: 0 件` と出す。CLI 表示は API の制限後の文をさらに 200 文字へ畳む。

store に `list_items(conn, *, include_terminal: bool, limit: int) -> tuple[list[dict], bool]` を足す。SQL は `limit + 1` 件を読み、`has_more` を決める。既存 `list_open` と `list_notes` の意味は変えない (`store/backlog.py:57-65`)。

### 5.3 `policy show`

`ops_policies.id` を削除キーとする。DB を唯一の正本とし、`policy/directives.md` は再生成可能な投影とする。file の行番号や directive 文面は識別子にしない。

管理行は次の ID 付きコメント block だけで識別する。ID は 10 進の `ops_policies.id`、directive は対応 DB row の `_render_directive()` 結果である。

```markdown
<!-- afx:managed-policy id=3 -->
- 週末の新規発注を禁止する
<!-- /afx:managed-policy id=3 -->
```

この開始・終了 marker が対で、ID が一致する block だけが managed である。marker 外の手書き行、旧 shell の legacy 行、不完全 / 不正 marker は byte 列のまま保持し、unmanaged かつ `removable=false` と表示する。DB と同じ文面が marker 外にあっても管理行に吸収しない。`policy add` も DB insert 後にこの marker 形式で投影する。再生成は認識済み managed block を全て除いた外側の byte 列を保持し、DB の `id ASC` で managed block を再構成し、同一 directory の temp file を fsync 後に replace する。

応答は次の固定 shape とする。

```json
{"managed":[{"id":3,"created_at":"2026-10-23T00:00:00+00:00","directive":"..."}],"unmanaged":[{"line":1,"directive":"...","removable":false}],"limit":200,"has_more":false}
```

`managed` は DB の `ops_policies` 行である。`unmanaged` は marker 外の手書き / legacy 行と不正 marker であり、常に `removable=false` と表示する。同文判定は廃止し、同一文の managed block と手書き行を ID marker で区別する。これらは `policy remove` では削除できない。

上限 `POLICY_SHOW_LIMIT=200` は managed と unmanaged の合計へ掛ける。managed は `id ASC`、unmanaged は file 順である。managed を先に budget へ入れる。残りへ unmanaged を入れる。超過時は `has_more=true` とする。directive は 4000 字の保存上限を保ち、CLI 表示だけ 200 字へ畳む。

CLI は次の形を固定する。

```text
管理対象:
  #3 2026-10-23T00:00:00+00:00 <directive>
管理外 (API から削除できません):
  line 1 <directive>
```

片方が 0 件でも見出しと `  0 件` を出す。

### 5.4 `policy remove <id>`

`id` は 1 以上の整数とする。存在する managed row だけを削除する。marker 外の手書き行と legacy shell 行は ID を持たないため削除できない。存在しない ID は `not_found` とする。

remove は durable mutation journal に accepted を書いた後、1 つの DB transaction で `DELETE FROM ops_policies WHERE id=?` と journal phase=`db_committed` を commit する。その後 `_regenerate_policy()` で file を replace し、成功時に phase=`projected` と固定 activity `SYSTEM policy_removed id=<id>` を保存する。DELETE commit と file replace は原子化できないため、replace 失敗は成功と報告せず terminal result=`outcome_unknown` を記録・応答する。commit 後・replace 前の crash で残る `db_committed` も起動時 recovery で terminal result=`outcome_unknown` にし、DB 全行から投影を再生成する。投影の回復状態と API の terminal result は分け、回復後も同じ key の result を成功へ書き換えない。起動時は未完 journal が無くても DB / file の marker ID が不一致なら再生成する。

add と同じ `operate`、idempotency key、autopilot 制限を使う (`service.py:699-718`)。同じ key の再送は成功時は保存済み `projected`、不明時は回復後も保存済み `outcome_unknown` を返す。別 key で削除済み ID を送ると `not_found` を返す。

### 5.5 principal × scope

| principal | `backlog list` | `policy show` | `policy remove` |
|---|---:|---:|---:|
| operator | 許可 (`guidance.read`) | 許可 (`guidance.read`) | 許可 (`operate`) |
| approver | 許可 | 許可 | 許可 |
| ops-bot | 拒否 | 拒否 | 拒否 |
| ops-web | 拒否 | 拒否 | 拒否 |
| ops-notify | 拒否 | 拒否 | 拒否 |

## 6. オフライン `afx status`

### 6.1 正本の分離

`afx status` は停止中診断である。daemon が稼働中なら次の 4 行だけを出し、DB、state file、activity log の詳細を読まない。

```text
agentic-fx offline status
daemon: active
note: この表示は観測時点の情報です。複数データ源の原子的 snapshot ではありません。
next: afx ctl status
```

終了コードは 0 とする。これにより稼働状態の正本は `afx ctl status` に一本化される。鍵が無く ctl を呼べない場合でも、offline status が稼働 daemon の state file を競合して読むことはしない。

`data/instance.lock` は `O_CREAT` なしで既存 file を開き、non-blocking shared lock を試みる。exclusive lock と競合して取れないときは `daemon: active`、shared lock を取れたときだけ `daemon: inactive` とし、state 詳細を読み終わるまで shared lock を保持する。file 不在、open / flock 失敗、保持者の不整合は `daemon: unknown` とし、state file は読まない。lock file や親 directory は作らない。判定前後の状態変化を含む TOCTOU はゼロと主張せず、上の「観測時点」固定文を active / inactive / unknown のすべてで出す。

### 6.2 データ源

| 出力 | データ源 | 読み方 |
|---|---|---|
| setup | `data/state/setup.json` | `read_setup_record(root)`。`ok / missing / corrupt` をそのまま出す (`store/setup_record.py:116-125`) |
| settings | `config/settings.yaml` | 他 source と分離して `ok / missing / invalid` を返す。missing / invalid でも後続 collector を止めない |
| pending | setup record、optional settings、optional DB、`.env` の鍵名、plugin inventory | source ごとの collector から `PendingFacts` を合成し、`pending_steps(..., for_status=True)` へ渡す。外部接続を probe しない (`setup/pending_facts.py:32-65`) |
| ops | `data/ops/<feature>.stage.json` と enroll marker | `read_stage(..., missing_ok=True)`、`read_marker()` (`ops_setup/stages.py:70-89,128-150`) |
| units | user unit file、systemd、linger | unit の有無と `is-active`。systemd が無ければ `unknown` |
| recent mission | readonly DB の `missions.recent(conn, 1)` | DB が無ければ `none`、破損 / schema 不足は `unavailable` (`store/missions.py:258`) |
| pending approvals | readonly DB の `approvals.pending(conn)` | 件数だけ出す (`store/approvals.py:263`) |
| plugin deploy ops | `count_open_readonly(db_path)` | `none / N / unavailable` を区別する |
| recent activity | `logs/activity.log` | `tail_lines(..., 1)`。本文は redactor を通し 200 字 |
| kill switch / mode | `data/state/state.json` と reset marker | 既存 instance lock の shared lock を保持できたときだけ読取専用 helper で読む。`StateStore.load()` は lock file を作るため使わない |

DB は `connect_readonly` だけを使う (`store/db.py:549`)。setup、settings、DB、state、ops、unit、activity、plugin はそれぞれ独立 collector とし、入力と失敗を共有しない。settings や DB が無いときはそれを要する pending fact だけ unknown / unavailable とし、鍵の存在、unit、ops marker、activity など残りは表示する。DB、lock、設定、unit、log、directory を作らない。初期化関数と migration を呼ばない。空 root、主要 file 不在、壊れた settings、DB 不在を含むすべての root で実行前後の directory tree (各 path、type、byte content) が同一であることを契約にする。

`collect_pending_facts` に keyword-only `service_active: bool | None = True` を足す。返す `PendingFacts.service_active` も `bool | None` とし、その値を渡す。daemon と wizard の既存呼び出しは既定 `True` を保つ。offline status は inactive 時 `False`、判定不能時 `None` を渡す。現在の固定 `service_active=True` は削除する (`setup/pending_facts.py:32-65`)。

### 6.3 停止中の固定出力

```text
agentic-fx offline status
daemon: inactive
setup: <ok|missing|corrupt>
settings: <ok|missing|invalid>
unit: afx.service=<installed|missing> active=<inactive|failed|unknown> linger=<enabled|disabled|unknown>
ops: bot=<stage|missing|invalid> web=<stage|missing|invalid> notify=<stage|missing|invalid> marker=<none|feature|invalid>
state: mode=<learning|paper|live|unavailable> kill_switch=<clear|latched|unavailable>
database: <ok|missing|unavailable>
recent mission: <#id state at|none|unavailable>
pending approvals: <N|unavailable>
plugin deploy ops: <N|none|unavailable>
recent activity: <timestamp category event|none|unavailable>
pending steps: <N>
  - <既存 render_pending_step の文>
note: この表示は観測時点の情報です。複数データ源の原子的 snapshot ではありません。
next: afx init | afx check | systemctl --user start afx.service | afx ctl status
```

`daemon: unknown` の場合も同じ形を出すが、`state: mode=unavailable kill_switch=unavailable` として state reader 自体を呼ばない。`next` は観測結果から 1 つだけ選ぶ。setup missing / corrupt は `afx init`、settings missing / invalid は `afx check`、unit があり daemon inactive は `systemctl --user start afx.service`、それ以外は `afx ctl status` とする。鍵 file は active / inactive / unknown の判定に使わず、鍵が一つも無くてもこの固定出力を出す。値が無いことと読めないことを混同しない。各 source の失敗で traceback を出さず、他の source の表示を続け、読めない節だけ `unavailable` にする。

## 7. 文書

### 7.1 原則の正本

原則の説明は `docs/operations/ops-api-2026-10-09.md` の冒頭に「CLI の置き場の原則」という 1 節を置く。7 群 × 2 レーンの表、ラベル、正所属、発注系 API が無いことを書く。

README はその節へリンクし、最初の使い分けだけを書く。`CLAUDE.md` は gitignore 対象で worktree に無いため正本にせず、release の手動同期対象とする。原則本文を README / CLAUDE.md へ複製しない。

### 7.2 更新対象

| 文書 | 更新 |
|---|---|
| `README.md` | `afx status` と `afx ctl status` の使い分け、原則節への link |
| `docs/operations/ops-api-2026-10-09.md` | 原則の正本、新 endpoint 3 本、scope、policy の managed / unmanaged、API に無い操作 |
| `docs/operations/first-run-acceptance.md` | `check backend`、`backlog add`、offline status、policy show/remove の手順と期待出力 |
| `docs/operations/improve-floor-revert.md` | status の選択と plugin 誘導 |
| `docs/superpowers/specs/2026-10-04-ops-api-design.md` | endpoint 25 → 28、route / scope 表 |
| `docs/superpowers/specs/2026-10-16-plugin-deploy-ops-design.md` | `plugin list` と status 誘導 |
| `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` | 現行運用名を `check backend` へ注記。履歴本文は改変せず追補 |
| `docs/superpowers/specs/*first-run-wizard*` | wizard が起動する argv と案内文 |
| `CLAUDE.md` (非追跡) | release 手動手順で「CLI の置き場の原則」を ops-api の正本節への 1 行 link に同期。`git check-ignore -q CLAUDE.md` と `test -f CLAUDE.md` を確認し、`rg -n 'CLI の置き場の原則|ops-api-2026-10-09' CLAUDE.md` で link を確認する。commit / AC の対象外 |

survey §2 の集計は 6 パターンで 248 行 / 46 ファイルであるが、一括置換はしない。現行の operations 文書、README、現行 spec のみを更新する。closed ticket、過去 plan、既存 spec の変更履歴は歴史資料として区別し、履歴本文は書き換えず必要なときだけ旧名の追補を足す。

`tests/acceptance/test_first_run_acceptance_doc.py:131` の文言 pin を更新する。実行可能な doc test は (a) ops-api 正本節の 7 群が固定順で出る、(b) README がその anchor へ link する、(c) 正式名 6 つ `afx status`、`afx ctl status`、`afx check backend`、`afx ctl backlog add`、`afx ctl policy show`、`afx plugin list` が正所属で出る、(d) 現行 README / `docs/operations/` の手順に `afx improve verify-backend` と正式名としての `afx ctl improve add` が 0 件、(e) local / ctl 各操作のラベルが記載される、を検査する。CLAUDE.md は非追跡なのでこの test に含めない。

## 8. 裁定の記録

### 8.1 確定した裁定

| 日付 | 裁定 | 本書への反映 |
|---|---|---|
| 2026-10-15 | daemon の生きた状態が要る操作は `afx ctl`、要らない操作は最上位 `afx` に置く | §1 のレーン定義、§2 の全表 |
| 2026-10-17 | 7 群 × 2 レーン、危険度は別ラベル、ctl は分類対象外、indicator / strategy は分けない | §0、§2、§3 の help |
| 2026-10-17 | 横断表示は正所属 1 つから誘導する | `status` を群 2、`plugin` を群 6 だけに掲載 |
| 2026-10-17 | help は分類メタデータから生成する | §3 の `surface.py` と一致 test |
| 2026-10-17 | (1)〜(6) の整理を行う | §2、§5、§6、§7 |

### 8.2 確認事項

製品方針として残るのは次の 1 件だけである。

1. **対話 shell の寿命。** 本書はこの release (`0.2.0`) で shell を残し、help 末尾に deprecated 固定文を 1 行出す。既定案は次 major (`1.0.0`) で素の TTY `afx` shell を削除し、`afx ctl` へ一本化することである。この削除方針だけはユーザー裁定として残す。

旧 (2) は技術決定で閉じた。§5.1 / §5.5 のとおり `logs.read` を流用せず新 scope `guidance.read` を operator / approver にだけ与え、ops-web / ops-bot / ops-notify には与えない。

## 9. 受入条件

上限 `L` を持つ条件は `L-1 / L / L+1` を入れて観測する。

| ID | 条件 | 観測点 |
|---|---|---|
| AC-1 | `afx --help` の stdout 全体が §3.2 と byte 一致し、7 群の順、左右レーン、local ラベルが固定される。choices に `improve` / `news` は出ない | stdout 全文 golden |
| AC-2 | `afx ctl help` が §3.3 と byte 一致し、末尾が「API に発注・SL 変更・クローズ・サービス停止は無い。」で終わる | stdout、rc=0 |
| AC-3 | argparse の全正式 local command、ctl `parse()` の全正式語順、`exposed=True` の surface record が一対一である。`exposed` なら `implemented`、planned なら非 exposed、alias / compatibility record は通常 parser と独立である | 独立 expected table と集合比較 |
| AC-4 | B1 / B2 完了後の最終統合で、API record の endpoint code / scope / mutating が `ENDPOINTS` と一致し、route 28 本が独立 `_EXPECTED_TABLE` と一致する。それまで新 record は help 非表示 | `ops/api_server.py:274` と routing test 2 か所、help snapshot |
| AC-5 | `[確認]` と `_CONFIRM_OPS`、`[approver]` と `_APPROVER_OPS` が一致する。`jobs cancel` は確認あり / approver なし | metadata と client 定数の集合比較 |
| AC-6 | argv shim の `afx improve verify-backend` は固定文を stderr に 1 回出し、rc=2、backend 接続・DB 作成・settings 書換えが 0 回である。`deprecated_since=0.2.0`、`remove_in=1.0.0` で、package version を 1.0.0 へ変異させると期限 pin が失敗する | shim spy、tmp dir、version monkeypatch |
| AC-7 | `afx ctl improve add x` と `afx ctl backlog add x` が同じ `Op("backlog_add", ["x"])`、同じ endpoint、同じ認可、同じ idempotency payload、同じ成功 / 失敗 rc になる。旧名だけ固定案内を stderr に出す | parse と fake API |
| AC-8 | `check` は外部接続 0 回で読み取り専用を保つ。`check backend` は §4.2 の非隔離 / 課金警告を stdout へ byte 一致で出し、`OPERATOR_SHELL_NO_ISOLATION` sentinel で指定 backend へ 1 往復し、DB を作らない。backend / provider の不正値は送信前 rc=2 | fake backend、sentinel spy、stdout、FS before/after |
| AC-9 | `plugin list` は §4.7 の source status、列、name / kind 順、live v1 + pending v2 + open rollback の併記、壊れた plugin、重複名、missing / unavailable を固定表示する。`--kind` の unknown 扱いも一意で、不在 DB / directory を作らない | source 直積 fixture、stdout golden、FS before/after |
| AC-10 | `backlog list` 既定は active 4 status だけを `updated_at DESC,id DESC` で返す。`--all` は done / rejected を含む。0 件の固定文を出す | API JSON と CLI stdout |
| AC-11 | backlog limit `L=500` に 499 / 500 / 501 を渡す。499 / 500 は受理し、501 は API 接続前 rc=2。DB に 99 / 100 / 101 件の fixture を置き、既定 100 と `has_more` を検査する | parse、SQL query count、応答 |
| AC-12 | API の idea / last_result は UTF-8 `L=4096` byte の 4095 / 4096 / 4097 で文字を壊さず固定印と truncated bool を返す。CLI の `L=200` 文字も 199 / 200 / 201 で切れ、改行と制御文字が 1 行を偽装しない | API JSON と stdout |
| AC-13 | `policy show` が ID marker 付き managed block と marker 外の `removable=false` を区別する。同一文の managed / 手書き行、legacy 行、不正 marker を取り違えない | 合成 DB と directives file |
| AC-14 | policy 合計上限 `L=200` について 199 / 200 / 201 件を入れ、順序、件数、`has_more` を検査する。directive 表示 `L=200` 字も 199 / 200 / 201 で検査する | API JSON と CLI stdout |
| AC-15 | `policy remove <id>` は該当 DB row だけを消し、DB の全 row から ID marker 付き block を再生成し、marker 外の手書き / legacy / 不正 marker を byte 保存し、固定 activity を 1 件残す | DB / file / journal / activity の before-after |
| AC-16 | remove の同一 idempotency key 再送は保存済み phase に応じた同じ応答になる。file replace 例外は `outcome_unknown` 応答を返す。別に DELETE + `db_committed` commit 後・file replace 前の hard crash を fault injection し、再起動が未完 phase を `outcome_unknown` と記録して DB 正本の file へ収束させる。journal がなくても marker ID 不一致から再生成する。別 key の削除済み IDと 0 / 負数 / 非数値は副作用なしで `not_found` / rc=2 | replace fault、crash child、再起動 recovery、API と client parse |
| AC-17 | principal × scope / 新 endpoint が §5.5 と一致する。`guidance.read` は operator / approver だけが持ち、ops-web / ops-bot / ops-notify は backlog / policy 自由文を取得できない。remove も operator / approver だけが通る | principal × scope × endpoint の直積 test |
| AC-18 | `afx status` は daemon active のとき §6.1 の 4 行だけを出し、DB / state / log reader を呼ばない。鍵不在でも同じ出力 | exclusive lock holder fixture、鍵無し、spy |
| AC-19 | daemon inactive で setup=`ok / missing / corrupt`、settings=`ok / missing / invalid`、主要 file 不在を固定語で出す。どの source が不良でも残りの collector と pending steps を表示する | source 直積 fixture |
| AC-20 | DB が ok / missing / corrupt、各 table が present / absent の場合に `ok / missing / unavailable`、mission、approval 件数、deploy op 件数を固定形で出す。DB を作成・migration せず、DB collector の失敗で settings / unit / ops / activity の表示を抑止しない | FS / sqlite fixture の直積 |
| AC-21 | 既存 instance lock へ shared lock を取って保持中だけ state file の clear / latched / markerあり / missing / corrupt を読む。`StateStore.load()` は呼ばず、shared lock 不可と lock 不在で state を読まない | flock fixture、state-reader spy |
| AC-22 | unit / systemd / linger、ops stage 3 feature、enroll marker、activity log が各々 present / missing / invalid の場合に該当節だけが変わる | source ごとの fixture と stdout snapshot |
| AC-23 | `collect_pending_facts(service_active=False)` が pending start を正しく残し、既存呼び出しの省略時は True を保つ。`None` も型と表示が壊れない | pending steps unit test |
| AC-24 | offline status の各 source が例外でも traceback を出さず、読めない節だけ `unavailable`、rc=1 になる。全 source が読めると rc=0 | source ごとの fault injection |
| AC-25 | shell help は `shell=True` から生成され、`backlog add`、`classified=False` の `stop`、deprecated 末尾 1 行を出す。旧 `improve add` は同一動作、`stop` は現行の graceful shutdown 要求になる | shell help snapshot、dispatch spy、既存 16 node の更新 |
| AC-26 | ops-api の 7 群順、README anchor link、正式名 6 つ、`plugin list`、local / ctl ラベルが存在し、現行手順の旧名が 0 件である。CLAUDE.md は release 手動確認 | `test_first_run_acceptance_doc.py` と追跡文書 grep pin |
| AC-27 | `news` は `planned=True, implemented=False, exposed=False` で、本 release の choices / help / parser / API endpoint のいずれにも出ない | full help golden と planned 例外 pin |
| AC-28 | daemon 稼働中の `ops enable` は rc=1 拒否へ変異されず、marker → stop → lock → enroll / install → unlock → restart の順で完了する。元が停止中なら再起動しない | 既存 durable stage test と mutation test |
| AC-29 | 空 root、主要 file 不在、settings 破損、DB 不在、鍵不在の active / inactive / unknown すべてで実行前後の tree が同一である | recursive tree manifest と byte compare |
| AC-30 | instance lock 不在 / unreadable は `daemon: unknown`、state reader 0 回となり、active / inactive / unknown すべてで「観測時点」固定文を出す | lock fault injection、stdout snapshot、spy |

## 10. 変更ファイルと task 表

### 10.1 変更ファイル予定

| 領域 | ファイル | 変更 |
|---|---|---|
| metadata | `src/agentic_fx/ops/surface.py` (新規) | 7 群、2 レーン、command record、help renderer |
| 最上位 CLI | `src/agentic_fx/entry.py`, `src/agentic_fx/backtest/cli.py`, plugin 表示 module | help epilog、`check backend`、廃止 shim、`plugin list`、`status` 配線 |
| offline status | `src/agentic_fx/status.py` (新規), `setup/pending_facts.py` | read-only collector、固定 renderer、`service_active` 引数 |
| ctl | `src/agentic_fx/ops/client.py` | 生成 help、正式名 / alias parse、list / show / remove formatter |
| API | `ops/contracts.py`, `ops/service.py`, `ops/api_server.py` | endpoint 3 本、service method、route 28 本 |
| store | `store/backlog.py`、必要なら policy 用の小 module | bounded list と policy read/delete |
| shell | `src/agentic_fx/commands.py` | `_HELP` 生成、backlog 正式名。新 surface は足さない |
| wizard | `src/agentic_fx/setup/wizard.py`, `setup/pending_text.py` | 新 argv と案内文 |
| test | `tests/test_entry_ctl.py`, `tests/ops/*`, `tests/backtest/*`, `tests/setup/*`, `tests/commands/*`, acceptance doc test | §9 の pin |
| docs | §7.2 の現行文書 | 原則、正式名、API、status |

変更予定を列挙するための表であり、本設計書更新ではこれらを変更しない。

### 10.2 task 表

| task | 依存 | 範囲 | 既存 pin (keep / update) | 新設 pin | 完了 AC |
|---|---|---|---|---|---|
| S1 metadata schema (非公開) | なし | `surface.py` の field / renderer interface、`guidance.read` | keep: `tests/ops/test_ctl_pins.py::test_client_does_not_import_setup_persistence_layer`。update: `tests/ops/test_scope_enforcement.py::{test_scope_table_matches_the_spec,test_every_endpoint_admits_exactly_the_principals_holding_its_scope}`、`tests/ops/test_ac56_57_ops_principals.py::{test_ac56_authorization_table_matches_the_spec_for_every_principal,test_ac56_every_route_is_allowed_only_by_the_spec_scope}` | new: exposed ⇒ implemented、planned ⇒ hidden、compatibility version 期限、classified=false shell record | AC-3、5、17、27 |
| B1 backlog API / parse | S1 | 正式 add / alias、bounded list、`guidance.read` | update: `tests/ops/test_ctl_operations.py::test_backlog_add_and_transition`、`tests/ops/test_ctl_pins.py::test_backlog_idea_echo_is_sanitized`、`::test_backlog_idea_echo_counts_the_removed_characters` | new: list order / limit / 4,096-byte truncation / alias equivalence / principal 直積。record はまだ `exposed=False` | AC-7、10〜12、17 |
| B2 policy API / parse | S1 | ID marker、DB 正本、durable journal、show / remove | keep: `tests/commands/test_improve_commands.py::test_policy_add_appends_to_directives_file`。update: route / endpoint の独立 table | new: 同文 managed / handmade、不正 marker、commit 後・replace 前 crash、`outcome_unknown`、再起動収束 | AC-13〜17 |
| A1 local check / plugin / ops | S1 | argv shim、`check backend`、`plugin list`、現行 `ops enable` | update: `tests/backtest/test_cli.py::{test_cli_improve_verify_backend_routes_to_verify_backend,test_cli_improve_verify_backend_routes_opencode_through_to_verify_backend,test_cli_verify_backend_prints_enable_guidance_for_opencode,test_cli_verify_backend_prints_detail_and_fingerprint_on_success,test_cli_improve_verify_backend_returns_1_when_verification_fails,test_cli_verify_backend_reports_ready_mismatch_as_rc1_not_traceback,test_cli_improve_verify_backend_requires_backend_argument}`、`tests/setup/test_wizard_llm.py::{test_opencode_needs_a_context_limit_and_is_never_marked_verified,test_the_improve_exchange_runs_as_a_child_command_after_the_commit}`、`tests/setup/test_wizard_guards.py::test_a_failed_improve_exchange_is_verify_failed_not_a_connection_error`。keep: `tests/ops_setup/test_stages.py::{test_enable_orders_marker_stop_confirm_lock_enroll_unlock_and_marker_removal,test_enable_with_afx_stopped_beforehand_does_not_start_it,test_enable_crash_at_each_point_resumes_and_clears_marker}` | new: shim 期限、非隔離 / 課金 stdout、sentinel spy、plugin view model 直積、稼働中 enable を rc=1 へする mutation killer | AC-6、8、9、28 |
| A2 offline status | S1 | source 独立 collector、shared lock、renderer | keep: `tests/setup/test_pending_facts.py::{test_collection_limits_history_pairs_and_journal_ids,test_supplied_landlock_availability_is_used_without_probing,test_a_failing_landlock_probe_is_unknown_not_unavailable,test_env_key_presence_combines_dotenv_and_process_environment}` | new: default `service_active=True` / explicit False / None と AC-18〜24 / 29 / 30 の lock、鍵無し、source fault、tree zero-write 直積 | AC-18〜24、29、30 |
| I1 help / shell / route 統合・文書公開 | B1, B2, A1, A2 | `exposed=True`、help golden、28 route 照合、shell、追跡文書 | update / keep は下の node 表のとおり | new: `afx --help` 全文 golden、shell `stop`、doc 7 群 / link / 6 正式名 / ラベル / 旧名ゼロ | AC-1〜5、25〜27 |

実装順は **S1 → B1 / B2 → A1 / A2 → I1** である。B1 / B2 と A1 / A2 は各々独立に test-first で完結できるが、新操作を help へ出すのは I1 だけである。A + B は I1 を含む 1 release とし、AC-4 の 28 route 照合も I1 で実行する。

#### I1 の既存 help / usage node

| 判定 | test node ID |
|---|---|
| update | `tests/test_entry_ctl.py::test_ctl_help_lists_operations` |
| update | `tests/setup/test_wizard_entry.py::test_the_help_describes_the_interactive_default` |
| keep | `tests/test_entry_ctl.py::test_ctl_without_operation_on_non_terminal_prints_usage_rc2` |
| keep | `tests/test_entry_ctl.py::test_ctl_unknown_operation_is_usage_rc2` |
| keep | `tests/test_entry_ctl.py::test_abbreviated_global_flag_before_ctl_is_usage_rc2` |
| keep | `tests/ops/test_ctl_pins.py::test_malformed_arguments_are_usage_rc2` |
| keep | `tests/ops/test_ctl_pins.py::test_no_operation_with_only_stdin_on_a_terminal_is_usage_rc2` |
| keep | `tests/ops/test_ctl_pins.py::test_no_operation_with_only_stdout_on_a_terminal_is_usage_rc2` |
| keep | `tests/ops/test_ctl_pins.py::test_unknown_principal_or_out_of_range_argument_is_usage_rc2` |
| keep | `tests/ops/test_ctl_pins.py::test_ctl_refuses_service_principals_without_touching_keys` |
| keep | `tests/ops/test_ctl_operations.py::test_out_of_range_argument_is_usage_before_any_key_or_connection` |
| keep | `tests/ops/test_ctl_ac20_28_53_terminal.py::test_ac28_no_operation_on_non_terminal_is_usage_rc2` |

12 本のうち 10 本は `usage: afx ctl` などの部分一致なので keep とし、全てを書き換える見積りにしない。endpoint 個数 pin は `tests/ops/test_ac01_02_43_api_routing.py::{test_ac1_all_24_endpoints_over_real_uds,test_ac1_routes_and_endpoints_match_the_independent_table_exactly}` を update し、production の `_ENDPOINT_COUNT` と同時に 28 へ上げる。

#### I1 の shell backlog / help node

`tests/commands/test_improve_commands.py` の次の 15 node はすべて **update** とし、正式入力を `backlog add` へ変える: `test_improve_add_creates_open_backlog_row`、`test_improve_add_without_text_returns_usage`、`test_improve_add_echoes_the_registered_idea_text`、`test_improve_add_warns_on_short_or_placeholder_idea`、`test_improve_add_does_not_block_registration_when_warned`、`test_improve_add_warning_text_does_not_leak_to_backlog_last_result`、`test_improve_add_warns_and_notes_removed_chars_for_zero_width_input`、`test_improve_add_strips_terminal_control_sequences_from_reply`、`test_improve_add_stores_idea_without_display_normalization`、`test_improve_add_does_not_warn_on_unclosed_bracket_prefix`、`test_improve_add_closing_bracket_must_be_at_the_end`、`test_improve_add_warns_exactly_at_length_boundary`、`test_improve_add_placeholder_detection_uses_normalized_display_not_raw_text`、`test_improve_add_does_not_leak_warning_or_display_into_activity_log`、`test_improve_add_warning_includes_reject_command_with_actual_bid`。

ops lane の 1 node `tests/ops/test_ac09_25_26_decision_lane.py::test_shell_policy_backlog_and_improve_go_through_the_lane` も **update** する。旧 `improve add` と正式 `backlog add` の同一動作は別の **new** parameterized node 1 本に集約する。shell help の `tests/test_commands.py::{test_approval_list_argument_boundaries,test_approval_retry_missing_id_is_value_error_with_help,test_help_includes_approval_list_journal_hint}` は **update** し、**new** の全文 snapshot と `stop` graceful-shutdown node を追加する。「22 本」という文字列出現数は test 数として使わない。

## 11. 後続と非目標

- `afx news` の parser、source 選択、429 backoff は `news-source-cli-and-429-backoff` で行う。
- 対話 shell の廃止は §8.2-1 のユーザー裁定後、次 major 向けの別束とする。本 release は deprecated 表示までである。
- bare `afx` の daemon 起動挙動は本束で変えない。
- policy の手書き行 / legacy 行を API 管理下へ移行する機能は作らない。
- backlog の検索、pagination cursor、CSV / JSON 出力 option は作らない。
- ops-web / bot / notify に backlog / policy の UI や通知を足さない。
- `guidance.read` 以外の新しい認可 scope は作らない。
- `verify-backend-isolated-via-service` は隔離適合の確認を service job で行う将来の `afx ctl` 操作として別束にする。本 spec の `afx check backend` は接続性だけで、隔離適合の合否を返さない。
- plugin 即時承認を API 化しない。
- 発注、SL 変更、クローズ、サービス停止の API を作らない。
- help で色、Unicode 罫線、端末幅ごとの動的折返しを行わない。

## 12. 残余リスク

- 対話 shell は API 認証を通らない。`ShellCaller.SHELL` の監査は残るが、利用者向けの「2 レーン」と実行境界が完全には一致しない。
- policy 投影は DB commit と file replace を同一 transaction にできない。durable phase、`outcome_unknown`、起動時再生成で収束させるが、crash 直後は一時的に旧投影が残り得る。
- offline status は複数 file / DB の同時 snapshot ではない。daemon 不在でも観測中に人間の別 CLI が変更すれば、節間で時刻がずれる。表示は診断用で、取引判断の正本にしない。
- instance lock が不在 / unreadable の環境では daemon 判定が `unknown` になる。unknown 時は state 詳細を読まず、他 source も「観測時点」の診断としてのみ出す。
- `news` は planned metadata だけに存在し、実装まで help に出ない。metadata だけが陳腐する可能性は後続束で解消する。

## 13. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-23 | v0.1 C0 | 7 群 × 2 レーンの完成表、metadata と help、例外裁定、新 API、offline status、文書、AC、2 束の task 表を新規作成 | `cli-surface-principle-and-reorg` の設計下書き | |
| 2026-10-23 | v0.2 | 設計レビュー 1 周目の 11 件、AC-1 / 3 / 4 / 6 / 8 / 9 / 12 / 13 / 15〜21 / 24〜30、依存順、test node 表を反映 | 設計レビュー 1 周目 sol、High 5 / Medium 6 全採用、§8.2-2 を技術決定で閉鎖 | |
