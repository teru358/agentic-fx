# afx / afx ctl コマンド面整理設計書

**版:** v0.1 C0 下書き

**対象:** `cli-surface-principle-and-reorg`

**前提:** 2026-10-17 のユーザー裁定で確定した「7 用途群 × 2 実行レーン」を実装可能な契約へ落とす。

**実装単位:** 束 A は分類メタデータ、help、ローカル CLI、文書を扱う。束 B は backlog / policy API を扱う。

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
- `afx news` の本体は `news-source-cli-and-429-backoff` で設計する。本書では群と help の予約行だけを定義する。
- 発注系 API は追加しない。
- `afx-ops <bot|web|notify>` は unit の入口であり、利用者向け操作面ではない。
- daemon や売買ロジックの動作は変えない。

## 1. 用語

| 用語 | 意味 |
|---|---|
| 群 | 利用者の目的による分類である。実装モジュール、動詞、危険度では分類しない。 |
| レーン | 同じ目的へ到達する実行境界である。左レーンは最上位 `afx`、右レーンは `afx ctl` である。 |
| ラベル | 操作の副作用と追加確認を示す表示である。`[読取]` は状態を変えない。`[変更]` は状態を変える。`[確認]` は端末確認を要する。`[approver]` は approver principal を要する。 |
| scope | API の認可単位である。表示ラベルとは別の機械契約である。主に `status.read`、`logs.read`、`operate`、`decide`、`local_guard` を使う (`ops/contracts.py:52-61`)。 |
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
| 導入・接続管理 | `afx` | `afx ops enable <bot\|web\|notify>` | `[変更]` | — | いいえ。サービス停止中のみ | `entry.py:52-59,80-85` |
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
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl backlog list [--all] [--limit N]` | `[読取]` | `logs.read` | はい | 新設 `client.py`; `service.py`; `store/backlog.py` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl backlog reject <id>` | `[変更]` | `operate` | はい | `client.py:934,1126`; `service.py:102-103` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl backlog reopen <id>` | `[変更]` | `operate` | はい | `client.py:934,1126`; `service.py:104-105` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl backlog note <id>` | `[変更]` | `operate` | はい | `client.py:934,1126`; `service.py:106-107` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl policy add <text>` | `[変更]` | `operate` | はい | `client.py:936,1142`; `service.py:108-109,699-718` |
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl policy show` | `[読取]` | `logs.read` | はい | 新設 `client.py`; `service.py` |
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

廃止名は `plugin bless` / `submit` の旧経路が固定案内で拒否する流儀に合わせる (`backtest/cli.py:567-597`)。互換別名は処理を継続するため別の API endpoint を作らない。別名そのものは分類表と通常 help に重複掲載せず、「互換名」節だけに載せる。

### 2.3 対話 shell

対話 shell はこの版で廃止しない。daemon 内から `ShellCaller.SHELL` として同じ ops 関数を呼ぶ既存の互換 UI と位置づける (`ops/contracts.py:76-83`)。第 3 のレーンとは呼ばない。

`commands.py:28-47` の手書き `_HELP` は削除する。分類メタデータのうち `shell=True` の操作から help を生成する。shell に実装されていない `events`、`jobs`、`whoami` は生成対象にしない。`improve add` は `backlog add` を正式表示にし、旧形を互換別名として受理する。新しい shell 専用語彙は足さない。将来の shell 廃止時期は §8.2 で確認する。

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
| `planned` | `news` のように別束で実装する予約行か |

`entry.py` は argparse の parser 群を従来どおり登録する。分類メタデータは parser 実装を置き換えない。表から `parser.epilog` を組み立て、`RawDescriptionHelpFormatter` で改行を固定する。`client.py` の静的 `USAGE` は `render_ctl_help()` の戻り値へ置き換える。parser とメタデータの全件一致を test で保証する。

### 3.2 `afx --help` の固定分類 block

argparse の usage、options、positional arguments の後へ、次の block をそのまま付ける。列幅は固定しない。文字列の各行を pin する。

```text
実行レーン:
  afx <sub>      daemon を介さない。停止中でも実行できる
  afx ctl <op>   稼働 daemon の認証・監査つき操作 API

用途                         daemon 不要                         稼働 daemon
導入・接続管理               init / check / keys / ops          ctl whoami
稼働観測・実行追跡           status                              ctl status / log / activity / events / jobs
市場入力を整える             news / history                     —
調査・評価する               backtest / analyze                 —
問い合わせ・改善運用         —                                   ctl ask / improve / backlog / policy / reflect retry
Plugin を採否・配備する      plugin                              ctl approval / approve / reject
保護状態を回復する           —                                   ctl killswitch / data resume

ラベル: [読取] 状態を変えない  [変更] 状態を変える  [確認] 端末確認  [approver] approver 権限
詳しい操作: afx <sub> --help / afx ctl help
```

`news` は `planned=True` でも予約行として出す。未実装の期間は `afx news --help` を作らず、README で「別束で追加予定」と明示する。

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
- API command の `endpoint_code`、route、scope、mutating を `ENDPOINTS` と比較する。`tests/ops/test_ac01_02_43_api_routing.py:251-288` の `_EXPECTED_TABLE` と同じ独立表の流儀を使う。
- `[読取]` は `ENDPOINTS[code].mutating is False` と一致させる。`[変更]` は `True` と一致させる。
- `[approver]` は `_APPROVER_OPS` と一致させる。`[確認]` は `_CONFIRM_OPS` と一致させる。`jobs cancel` の差を pin する。
- `surface.py` の `scope` は `ENDPOINTS[code].scope` と完全一致させる。
- endpoint は 3 本増えて 25 から 28 になる。`ops/api_server.py:274-281`、`tests/ops/test_ac01_02_43_api_routing.py:91,282` の個数 pin と `_EXPECTED_TABLE` を同時に更新する。
- `approval_change` は CLI の詳細表示が内部で使う endpoint だが、独立コマンドではない。分類漏れとは扱わない。

## 4. レーン規則に合わないものの裁定

### 4.1 `keys` と `ops enable`

最上位レーンに残す。daemon の生きた状態を操作するのではなく、鍵と unit 資材をローカルで整える操作だからである。ただし「停止中でも可」ではなく「停止中のみ」である。

群や危険度ラベルを増やさない。metadata の `condition="停止中のみ"` と各 subcommand help へ同じ文言を出す。実装は稼働を検知したら暗黙に停止せず、何も変更せず rc=1 で拒否する。

### 4.2 `verify-backend`

`afx check` の引数なし形はローカル読み取り専用のままにする。ネットワーク往復とサブスク枠を使う検査は `afx check backend` という明示サブコマンドへ分離し、`[変更]` と表示する。

`backend` がオンライン検査であることはコマンド名と help 文「実 backend へ 1 往復する。サブスク枠を使う」で明示できる。二重の確認になる `--online` は足さない。現在の `ensure_initialized` と DB 初期化は不要なので外す (`backtest/cli.py:822,839`)。settings と `.env` は読む。scheduler、wave、backlog、DB は変更しない。

### 4.3 `plugin bless`

最上位レーンに残す。これは daemon の approval queue を決定する API 操作ではなく、人間が `_human` 候補をローカルで検証し、即時配備する明示経路である (`backtest/cli.py:136-141`)。

help は「ローカル即時承認。API principal の確認は通らない。plugin の journal / approval / activity 監査は残る」と書く。`afx ctl approve` の代替であるかのように説明しない。`--from _human` なしの旧経路は従来どおり固定案内で拒否する。

### 4.4 `jobs cancel`

`[変更] [確認]` とする。scope は `jobs.own` のままにする。operator が自分の job を取り消す操作であり、approver の決定ではない。`_CONFIRM_OPS` には含め、`_APPROVER_OPS` には含めない (`client.py:879-881`)。

### 4.5 `whoami`

「導入・接続管理」の ctl レーンを正所属とする。認証した接続先と principal を検査する目的だからである。daemon が止まっていると使えない事実はレーン説明で示す。稼働観測へ重複掲載しない。

### 4.6 別名、廃止名、素の `afx`、対話 shell

別名と廃止名は §2.2 の 2 種に分ける。廃止名は parser の通常 command record へ入れず、期限つき compatibility record にする。リリース番号を record に持たせず、削除予定の release をコメントと変更履歴へ記録する。

素の `afx` と `afx --daemon` はサービス入口なので 7 群へ入れない (`entry.py:92-98`)。この版では起動挙動を変えない。最上位 help の「実行レーン」は subcommand の分類であり、daemon executable 自体の説明ではない。

対話 shell は §2.3 のとおり互換 UI として残す。`_HELP` の重複だけを除く。shell が API 認証を通らない点は隠さず、`ShellCaller.SHELL` の既存監査境界であると運用文書へ書く。

## 5. 新設 API

### 5.1 endpoint

| code | route | scope | mutating | idempotent | autopilot 制限 |
|---|---|---|---|---|---|
| `backlog_list` | `GET /v1/backlog` | `logs.read` | false | — | — |
| `policy_show` | `GET /v1/policy` | `logs.read` | false | — | — |
| `policy_remove` | `POST /v1/policy/{id}/remove` | `operate` | true | true | true |

`api_server.py` は現在 GET / POST だけを受ける (`api_server.py:283-286`)。remove も既存の idempotency と監査の枠へ載せるため POST とする。endpoint 総数は 28 になる。

読取は `status.read` ではなく `logs.read` とする。backlog の idea と policy directive は利用者の自由文である。自由文を返す `log` / `activity` と同じ開示範囲が適切である。`status.read` を使うと ops-bot に本文が開く。新 scope は作らず既存 principal 表の変更を避ける (`ops/contracts.py:89-101`)。

bot、notify、公開 web の機能には新操作を配線しない。ops-bot と ops-notify は `logs.read` を持たない。ops-web は既存 `logs.read` を持つため credential 上は endpoint を呼べるが、本束では web route と UI を作らない。この意味の確認を §8.2 に残す。

### 5.2 `backlog list`

既定は活動中の `open`、`observation`、`note`、`selected` を返す。`--all` は `done` と `rejected` も加える。並びは `updated_at DESC, id DESC` とする。既定上限は 100、`--limit` の許容範囲は 1〜500 とする。

応答は次の固定 shape とする。

```json
{"items":[{"id":12,"status":"open","source":"user","updated_at":"2026-10-23T00:00:00+00:00","idea":"...","last_result":null}],"limit":100,"has_more":false}
```

CLI は 1 行 1 件で `#<id> <status> <source> <updated_at> <idea>` を出す。`last_result` があるときだけ次行へ 2 空白 indent で出す。0 件は `backlog: 0 件` と出す。idea と last_result は既存 redactor / display_text を通し、改行を 1 空白へ畳み、それぞれ 200 字を上限とする。

store に `list_items(conn, *, include_terminal: bool, limit: int) -> tuple[list[dict], bool]` を足す。SQL は `limit + 1` 件を読み、`has_more` を決める。既存 `list_open` と `list_notes` の意味は変えない (`store/backlog.py:57-65`)。

### 5.3 `policy show`

`ops_policies.id` を削除キーとする。file の行番号は手書き行で動くため識別子にしない (`service.py:728-737`)。

応答は次の固定 shape とする。

```json
{"managed":[{"id":3,"created_at":"2026-10-23T00:00:00+00:00","directive":"..."}],"unmanaged":[{"line":1,"directive":"...","removable":false}],"limit":200,"has_more":false}
```

`managed` は DB の `ops_policies` 行である。`unmanaged` は `policy/directives.md` にあるが DB record と対応しない手書き行と legacy shell 行である。`unmanaged` は常に `removable=false` と表示する。同じ文面が managed と unmanaged にある場合は現行 `_regenerate_policy` の `owned` 集合と同じ比較を使う。曖昧な同文行を削除可能と推測しない。

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

`id` は 1 以上の整数とする。存在する managed row だけを削除する。手書き行と legacy shell 行は ID を持たないため削除できない。存在しない ID は `not_found` とする。

処理は `_mutate` の中で `DELETE FROM ops_policies WHERE id=?`、commit、`_regenerate_policy()`、activity の順に行う。activity は自由文を含めず `SYSTEM policy_removed id=<id>` とする。add と同じ `operate`、idempotency key、autopilot 制限を使う (`service.py:699-718`)。同じ idempotency key の再送は保存済み結果を返す。別 key で削除済み ID を送ると `not_found` を返す。

### 5.5 principal × scope

| principal | `backlog list` | `policy show` | `policy remove` |
|---|---:|---:|---:|
| operator | 許可 (`logs.read`) | 許可 (`logs.read`) | 許可 (`operate`) |
| approver | 許可 | 許可 | 許可 |
| ops-bot | 拒否 | 拒否 | 拒否 |
| ops-web | scope 上は許可。ただし UI 非公開 | scope 上は許可。ただし UI 非公開 | 拒否 |
| ops-notify | 拒否 | 拒否 | 拒否 |

## 6. オフライン `afx status`

### 6.1 正本の分離

`afx status` は停止中診断である。daemon が稼働中なら次の 3 行だけを出し、DB、state file、activity log の詳細を読まない。

```text
agentic-fx offline status
daemon: active
next: afx ctl status
```

終了コードは 0 とする。これにより稼働状態の正本は `afx ctl status` に一本化される。鍵が無く ctl を呼べない場合でも、offline status が稼働 daemon の state file を競合して読むことはしない。

daemon 不在は既存 `data/instance.lock` の保持者を読み取り専用で調べる。lock file や親 directory を作らない。判定不能なら `daemon: unknown` とし、詳細は安全に読める静的材料だけへ限定する。

### 6.2 データ源

| 出力 | データ源 | 読み方 |
|---|---|---|
| setup | `data/state/setup.json` | `read_setup_record(root)`。`ok / missing / corrupt` をそのまま出す (`store/setup_record.py:116-125`) |
| pending | setup record、settings、DB、`.env` の鍵名、plugin inventory | `collect_pending_facts` → `pending_steps(..., for_status=True)`。外部接続を probe しない (`setup/pending_facts.py:32-65`) |
| ops | `data/ops/<feature>.stage.json` と enroll marker | `read_stage(..., missing_ok=True)`、`read_marker()` (`ops_setup/stages.py:70-89,128-150`) |
| units | user unit file、systemd、linger | unit の有無と `is-active`。systemd が無ければ `unknown` |
| recent mission | readonly DB の `missions.recent(conn, 1)` | DB が無ければ `none`、破損 / schema 不足は `unavailable` (`store/missions.py:258`) |
| pending approvals | readonly DB の `approvals.pending(conn)` | 件数だけ出す (`store/approvals.py:263`) |
| plugin deploy ops | `count_open_readonly(db_path)` | `none / N / unavailable` を区別する |
| recent activity | `logs/activity.log` | `tail_lines(..., 1)`。本文は redactor を通し 200 字 |
| kill switch / mode | `data/state/state.json` と reset marker | daemon 不在を確定した後だけ `StateStore.load(deadline=...)`。競合・破損は `unavailable` (`store/state.py:175-208`) |

DB は `connect_readonly` だけを使う (`store/db.py:549`)。DB、lock、設定、unit、log を作らない。初期化関数と migration を呼ばない。

`collect_pending_facts` に keyword-only `service_active: bool = True` を足す。返す `PendingFacts.service_active` へその値を渡す。daemon と wizard の既存呼び出しは既定 `True` を保つ。offline status は `False`、判定不能時は `None` を渡す。現在の固定 `service_active=True` は削除する (`setup/pending_facts.py:32-65`)。

### 6.3 停止中の固定出力

```text
agentic-fx offline status
daemon: inactive
setup: <ok|missing|corrupt>
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
next: afx init | afx check | systemctl --user start afx.service | afx ctl status
```

`next` は観測結果から 1 つだけ選ぶ。setup missing / corrupt は `afx init`、fatal な設定は `afx check`、unit があり daemon inactive は `systemctl --user start afx.service`、それ以外は `afx ctl status` とする。値が無いことと読めないことを混同しない。各 source の失敗で traceback を出さず、他の source の表示を続け、読めない節だけ `unavailable` にする。state の安全な読取が失敗したときは mode や kill switch を推測しない。

## 7. 文書

### 7.1 原則の正本

原則の説明は `docs/operations/ops-api-2026-10-09.md` の冒頭に「CLI の置き場の原則」という 1 節を置く。7 群 × 2 レーンの表、ラベル、正所属、発注系 API が無いことを書く。

README はその節へリンクし、最初の使い分けだけを書く。CLAUDE.md の「CLI の置き場の原則」1 行は同じ文へ同期する。原則本文を 3 か所へ複製しない。

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
| `CLAUDE.md` | 原則の 1 行だけ同期。実体が別管理なら実装時に所在を確認 |

survey §2 の集計は 6 パターンで 248 行 / 46 ファイルである。closed ticket と過去 plan は歴史資料なので一括置換しない。現行 operations、現行 spec、README を更新し、過去文書には必要なときだけ「旧名」注記を足す。

`tests/acceptance/test_first_run_acceptance_doc.py:131` の文言 pin を更新する。doc test は少なくとも `afx status`、`afx ctl status`、`afx check backend`、`afx ctl backlog add`、`afx ctl policy show` を正しいレーンの記述として検査する。

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

以下だけが製品方針として残る。実装技術で決まる事項は本文で決めた。

1. **対話 shell の寿命。** 本書はこの版で shell を残し、help の正本だけを統合する。2 レーンを利用者面として厳密にするなら、次の major で素の TTY `afx` shell を廃止し、`afx ctl` へ一本化するか。
2. **「web には出さない」の強さ。** 本書は web UI に配線しない意味とした。既存 `ops-web` は `logs.read` を持つため credential 上は `backlog list` / `policy show` を呼べる。ops-web principal 自体も認可で拒否する必要があるなら、`logs.read` 流用をやめて新 scope または endpoint ごとの principal deny が必要である。

## 9. 受入条件

上限 `L` を持つ条件は `L-1 / L / L+1` を入れて観測する。

| ID | 条件 | 観測点 |
|---|---|---|
| AC-1 | `afx --help` に §3.2 の block が byte 一致で 1 回出る。7 群の順と左右レーンが固定される | stdout 全体から block を抽出して比較 |
| AC-2 | `afx ctl help` が §3.3 と byte 一致し、末尾が「API に発注・SL 変更・クローズ・サービス停止は無い。」で終わる | stdout、rc=0 |
| AC-3 | argparse の全正式 local command、ctl `parse()` の全正式語順、surface record が一対一である。alias と planned row は独立集合である | 独立 expected table と集合比較 |
| AC-4 | API record の endpoint code / scope / mutating が `ENDPOINTS` と一致する。route 28 本が独立 `_EXPECTED_TABLE` と一致する | `ops/api_server.py:274` と routing test 2 か所 |
| AC-5 | `[確認]` と `_CONFIRM_OPS`、`[approver]` と `_APPROVER_OPS` が一致する。`jobs cancel` は確認あり / approver なし | metadata と client 定数の集合比較 |
| AC-6 | `afx improve verify-backend` は固定文を stderr に 1 回出し、rc=2、backend 接続・DB 作成・settings 書換えが 0 回である | spy と tmp dir |
| AC-7 | `afx ctl improve add x` と `afx ctl backlog add x` が同じ `Op("backlog_add", ["x"])`、同じ endpoint、同じ認可、同じ idempotency payload、同じ成功 / 失敗 rc になる。旧名だけ固定案内を stderr に出す | parse と fake API |
| AC-8 | `check` は外部接続 0 回で読み取り専用を保つ。`check backend` は指定 backend へ 1 往復し、DB を作らない。backend / provider の不正値は送信前 rc=2 | fake backend、FS before/after |
| AC-9 | `plugin list` が deployed、pending、version、未完 deploy op を name / kind ごとに表示し、`--kind` で indicator / strategy を絞る。DB 不在でも DB を作らない | readonly fixture と FS before/after |
| AC-10 | `backlog list` 既定は active 4 status だけを `updated_at DESC,id DESC` で返す。`--all` は done / rejected を含む。0 件の固定文を出す | API JSON と CLI stdout |
| AC-11 | backlog limit `L=500` に 499 / 500 / 501 を渡す。499 / 500 は受理し、501 は API 接続前 rc=2。DB に 99 / 100 / 101 件の fixture を置き、既定 100 と `has_more` を検査する | parse、SQL query count、応答 |
| AC-12 | backlog idea / last_result の表示上限 `L=200` が 199 / 200 / 201 字で切れ、改行と制御文字が 1 行を偽装しない | stdout |
| AC-13 | `policy show` が managed の安定 ID と unmanaged の `removable=false` を区別する。同文、手書き、legacy 行を誤って managed としない | 合成 DB と directives file |
| AC-14 | policy 合計上限 `L=200` について 199 / 200 / 201 件を入れ、順序、件数、`has_more` を検査する。directive 表示 `L=200` 字も 199 / 200 / 201 で検査する | API JSON と CLI stdout |
| AC-15 | `policy remove <id>` は該当 DB row だけを消し、managed 行を再生成し、手書き / legacy 行を残し、固定 activity を 1 件残す | DB / file / activity の before-after |
| AC-16 | remove の同一 idempotency key 再送は同じ成功応答になる。別 key の削除済み IDと 0 / 負数 / 非数値は副作用なしで `not_found` / rc=2 になる | API と client parse |
| AC-17 | principal × 新 endpoint の許可 / 拒否が §5.5 と一致する。ops-bot / ops-notify は自由文を取得できず、remove は operator / approver だけが通る | principal × endpoint の直積 test |
| AC-18 | `afx status` は daemon active のとき §6.1 の 3 行だけを出し、DB / StateStore / log reader を呼ばない | lock holder fixture と spy |
| AC-19 | daemon inactive で setup が ok / missing / corrupt の各場合を固定語で出す。setup 不良でも残りの安全な source を表示する | setup fixture 3 種 |
| AC-20 | DB が ok / missing / corrupt、各 table が present / absent の場合に `ok / missing / unavailable`、mission、approval 件数、deploy op 件数を固定形で出す。DB を作成・migration しない | FS / sqlite fixture の直積 |
| AC-21 | state file が clear / latched / markerあり / missing / corrupt / lock timeout の各場合に推測せず固定語を出す | StateStore fixture |
| AC-22 | unit / systemd / linger、ops stage 3 feature、enroll marker、activity log が各々 present / missing / invalid の場合に該当節だけが変わる | source ごとの fixture と stdout snapshot |
| AC-23 | `collect_pending_facts(service_active=False)` が pending start を正しく残し、既存呼び出しの省略時は True を保つ。`None` も型と表示が壊れない | pending steps unit test |
| AC-24 | offline status の各 source が例外でも traceback を出さず、読めない節だけ `unavailable`、rc=1 になる。全 source が読めると rc=0 | fault injection |
| AC-25 | shell help は surface metadata の `shell=True` だけから生成される。`backlog add` を正式表示し、旧 `improve add` は同一動作になる | shell help snapshot と既存 22 test の更新 |
| AC-26 | operations 文書の原則節、README link、first-run の正式名が存在し、旧名を現行手順として出さない | `test_first_run_acceptance_doc.py` と文書 grep pin |
| AC-27 | `news` は最上位分類 block に 1 回出るが、本束で parser / 実装 / API endpoint を作らない | help と parser の planned 例外 pin |

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

変更予定を列挙するための表であり、本 C0 ではこれらを変更しない。

### 10.2 task 表

| 束 / task | 依存 | 範囲 | test 完成形の要点 | 規模 |
|---|---|---|---|---|
| A1 分類メタデータと help | なし | `surface.py`、`entry.py`、`client.py`、shell help | AC-1〜5、25、27。独立 expected table から全件、順序、scope、label を照合 | M |
| A2 check / plugin | A1 | `check backend`、旧名 1 版 shim、`plugin list` | AC-6、8、9。外部往復回数、DB 非作成、旧名副作用ゼロ | M |
| A3 offline status | A1 | readonly collector、renderer、pending facts 引数 | AC-18〜24。source 単位の fault injection と snapshot | L |
| A4 文書 | A1〜A3 | README、operations、現行 spec、CLAUDE.md 同期 | AC-26。文書 pin と旧名の現行手順ゼロ | M |
| B1 backlog API / CLI | A1 | 正式 add / alias、list store / endpoint / client | AC-7、10〜12、17。limit、order、自由文、principal 直積 | M |
| B2 policy API / CLI | A1 | show / remove、managed / unmanaged、再生成、監査 | AC-13〜17。ID、同文、idempotency、autopilot、principal 直積 | L |

A1 → A2 / A3 → A4 を束 A とする。B1 と B2 は A1 の metadata interface に依存するが、互いには依存しない。実装時は各 task を test-first で完結させる。

survey が数えた既存 pin の更新対象を見積りへ固定する。

- help / usage pin: 12 本。
- `verify-backend` の argv / 文言 pin: 9 本。
- `ctl improve add` の pin: 4 本。
- shell で同名を使う pin: 22 本。shell を残す本案では正式名を `backlog add` へ直し、旧名同一動作の代表 test だけ残す。
- shell `_HELP` 自体の既存 pin: 上の 22 本とは別に 3 本が該当する。生成 help の snapshot へ集約する。
- endpoint 個数 pin: `api_server.py:274` と test 2 か所の計 3 か所。25 を 28 にする。

## 11. 後続と非目標

- `afx news` の parser、source 選択、429 backoff は `news-source-cli-and-429-backoff` で行う。
- 対話 shell の廃止は §8.2 の裁定後に別束とする。
- bare `afx` の daemon 起動挙動は本束で変えない。
- policy の手書き行 / legacy 行を API 管理下へ移行する機能は作らない。
- backlog の検索、pagination cursor、CSV / JSON 出力 option は作らない。
- ops-web / bot / notify に backlog / policy の UI や通知を足さない。
- 新しい認可 scope は作らない。§8.2 の回答が認可拒否を要求した場合だけ再設計する。
- plugin 即時承認を API 化しない。
- 発注、SL 変更、クローズ、サービス停止の API を作らない。
- help で色、Unicode 罫線、端末幅ごとの動的折返しを行わない。

## 12. 残余リスク

- `ops-web` は `logs.read` を持つため、新 endpoint を UI に出さなくても credential があれば自由文を取得できる。§8.2 の解釈が確定するまで残る。
- 対話 shell は API 認証を通らない。`ShellCaller.SHELL` の監査は残るが、利用者向けの「2 レーン」と実行境界が完全には一致しない。
- `policy show` の unmanaged 判定は rendered text の集合差である。同一文面の手書き行と managed 行を人間の意図まで区別できない。削除可能と誤表示しない側へ倒す。
- offline status は複数 file / DB の同時 snapshot ではない。daemon 不在でも観測中に人間の別 CLI が変更すれば、節間で時刻がずれる。表示は診断用で、取引判断の正本にしない。
- `/proc/locks` を使えない環境では daemon 判定が `unknown` になる。unknown 時に state / DB 詳細を出しすぎないことで競合を避ける。
- `news` は help に予約表示されるが本束では未実装である。README の予定注記がないと利用者が実装済みと誤解する。
- 旧 `improve verify-backend` の削除時期を release 作業で落とす恐れがある。compatibility record と release checklist を test だけでは自動期限切れにできない。

## 13. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-23 | v0.1 C0 | 7 群 × 2 レーンの完成表、metadata と help、例外裁定、新 API、offline status、文書、AC、2 束の task 表を新規作成 | `cli-surface-principle-and-reorg` の設計下書き | |
