# afx / afx ctl コマンド面整理設計書

**版:** v0.5 (policy の安全な照合・移行・読取 API と明示 reconcile、alias 期限、help の正本、argv shim の前置き flag)

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
| help の正本 | `src/agentic_fx/cli_surface.py` (ops package の外) に純データの分類メタデータを置く。argparse の `help` / `description` / `epilog`、葉操作の説明、`afx ctl help`、対話 shell の `_HELP` をそこから作る。 |
| `verify-backend` | 正式名を `afx check backend --backend <name> [--provider chatgpt]` にする。`check` の引数なし形は従来どおりローカルの読み取り専用検査とする。`backend` という明示サブコマンドが実往復を示すため `--online` は足さない。 |
| backlog | 正式名を `afx ctl backlog add <idea>` にする。`improve add` は互換別名として残す。`backlog list [--all] [--limit N]` を足す。 |
| policy | `afx ctl policy show`、`afx ctl policy remove <id>`、`afx ctl policy reconcile` を足す。`<id>` は安定した `ops_policies.id` とする。`show` は純粋な読取で、投影 file と DB のずれを `projection` (`ok` / `stale` と理由) で示すだけにする。修復は明示操作 `reconcile` で行う。 |
| plugin | `afx plugin list [--kind indicator\|strategy]` を足す。indicator と strategy を別の用途群へ分けない。 |
| ops 機能 | `afx ops <bot\|web\|notify> <enable\|disable\|reconfigure\|status>` の 機能 → 動詞 の階層にする。`reconf` は `reconfigure` の別名である。旧形 `afx ops enable\|disable\|status <feature>` は 1 リリースだけ案内つきの互換別名として受け付ける。本 release で `reconfigure` を出すのは web だけで、bot / notify には出さない。 |
| offline status | `afx status` を足す。instance lock が保持されている間は詳細を直接読まず、保持者の手がかりに合わせた次の一手へ誘導する。lock が空いているときだけローカル記録を fail-soft に読む。 |
| API の非目標 | 発注、SL 変更、ポジションのクローズ、サービス停止の API は作らない。 |

### 0.1 範囲外

- ウィザード対話の見栄えは別束である。
- `afx news` の本体は `news-source-cli-and-429-backoff` で設計する。本書では群の planned metadata だけを定義し、実装まで help には出さない。
- 発注系 API は追加しない。
- `afx-ops <bot|web|notify>` は unit の入口であり、利用者向け操作面ではない。
- daemon や売買ロジックの動作は変えない。
- `afx ops <feature> reconfigure` の動作本体 (afx に触れない再設定) は `docs/superpowers/specs/2026-10-23-web-lan-access-design.md` が持つ。本書は階層、別名、help、文書の置換だけを定める。同 spec が本体を作るのは web だけであり、bot / notify の `reconfigure` は本 release に無い。

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
| 互換別名 | 正式名と同じ処理、引数、認可、確認、終了コードへ正規化される旧名である。旧名から来たことは表示用の印だけに残す。期限つきの互換別名は `deprecated_since` / `remove_in` を持ち、毎回 stderr に固定の移行案内を出す。 |
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
| 導入・接続管理 | `afx` | `afx ops <bot\|web\|notify> enable` | `[変更]` | — | いいえ。必要時だけ daemon を一時停止し、元が稼働中なら再開 | `entry.py:49-55,81-86`; `ops_setup/stages.py` |
| 導入・接続管理 | `afx` | `afx ops <bot\|web\|notify> disable` | `[変更]` | — | いいえ | `entry.py:49-55,85-86` |
| 導入・接続管理 | `afx` | `afx ops web reconfigure` (別名 `reconf`) | `[変更]` | — | いいえ。afx に触れない | 本体は `2026-10-23-web-lan-access-design.md` の `run_ops_reconfigure`。本書は parser と help だけ。bot / notify は `implemented=False, exposed=False` |
| 導入・接続管理 | `afx` | `afx ops <bot\|web\|notify> status` | `[読取]` | — | いいえ | `entry.py:49-55,85-86` |
| 導入・接続管理 | `afx` | `afx ops enable\|disable\|status <feature>` **(期限つき互換別名、1 リリースのみ)** | 新形と同じ | — | 新形と同じ | `entry.py` の argv shim で新形へ書き換える |
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
| 問い合わせ・改善運用 | `afx ctl` | `afx ctl policy reconcile` | `[変更]` | `operate` | はい | 新設 `client.py`; `service.py`; `policy_projection.py` |
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

現行 24 + 24 の数え方は survey §1 を踏襲する。`ops` の 3 操作は語順だけが変わり、数は変わらない。`ops web reconfigure` と `policy reconcile` は新しい操作であり、現行の数には含めない。`killswitch reset` は表示形と変更形を別の完成形として表へ出す。`help` は表示機能、`stop` は拒否用の罠、素の `afx` / `afx --daemon` はサービス入口なので分類対象へ数えない。

最上位の `improve` 群は廃止する。上表の `afx improve verify-backend` は分類上の正式所属ではなく、1 リリースだけ存在する廃止名の記録である。通常 help の左列には `improve` を出さない。

### 2.2 別名と廃止名

| 入力 | 扱い | 固定文言 | 終了コード |
|---|---|---|---|
| 先頭語が `improve` の argv すべて (`afx improve`、`afx improve --help`、`afx improve verify-backend …` を含む) | 1 リリースだけ残す廃止名。新処理を呼ばない | ``afx improve verify-backend` は廃止されました。`afx check backend --backend <name> [--provider chatgpt]` を使ってください。` | 2 |
| `afx ctl improve add <idea>` | `backlog add` の互換別名。正式名と同じ `Op("backlog_add")` へ正規化し、`Op.via_alias="improve add"` の印を付ける | ``afx ctl improve add` は旧名です。`afx ctl backlog add` を使ってください。` | 正式名と同じ。案内は stderr |
| `afx ops <enable\|disable\|status> <bot\|web\|notify> …` | 期限つき互換別名。argv shim が `afx ops <feature> <verb> …` へ書き換えて新形と同じ処理を呼ぶ | ``afx ops <verb> <feature>` は旧形です。`afx ops <feature> <verb>` を使ってください。この形は 0.3.0 で削除します。` (`<verb>` と `<feature>` は入力の語で置換する) | 新形と同じ。案内は stderr に 1 回 |
| `afx ops <enable\|disable\|status>` の後に機能名が無い、または不正 | 期限つき互換別名の誤用。新処理を呼ばない | 上と同じ案内の `<feature>` を `<bot\|web\|notify>` にした文 | 2 |
| `afx ops <feature> reconf …` | `reconfigure` の恒久的な短縮別名。argv shim が `reconfigure` へ書き換える | なし | 新形と同じ |
| `approvals list` / `approvals show` | 既存互換別名として維持 | なし | 正式名と同じ |
| `approval <id>` | `approval show <id>` の既存短縮として維持 | なし | 正式名と同じ |
| `retry <id>` | `approval retry <id>` の既存短縮として維持 | なし | 正式名と同じ |

廃止名は `plugin bless` / `submit` の旧経路が固定案内で拒否する流儀に合わせる (`backtest/cli.py:567-597`)。`afx improve verify-backend` は argparse へ `improve` parser を登録せず、`entry.main()` の parser 構築・`parse_args()` より前の argv shim だけで捕捉する。shim は先頭語が `improve` の argv をすべて捕捉し、同じ固定案内を出す。`afx improve` 単独や `afx improve --help` も argparse の `invalid choice` にはならない。このため positional choices、`afx --help`、分類 metadata の通常 record のいずれにも `improve` は現れない。

argv shim はすべて、現行 `entry.main()` と同じ方法で command head を決めてから適用する (`entry.py:19-25`)。先頭から連続する完全一致の `--daemon` を何個でも読み飛ばし、その次の位置を head とする。`improve` / `ops` の判定、`ops` の 2 語目と 3 語目の入替え、`reconf` の書換えは、すべて head からの相対位置で行う。head より前の `--daemon` は消さずにそのまま残す。head が `ctl` なら従来どおり client へ渡し、shim は何もしない。`--dae` のような略記は読み飛ばさず、現行どおり argparse の usage error にする。したがって `afx --daemon improve --help` は `afx improve --help` と同じ固定案内と rc=2 になり、`afx --daemon ops enable web` は `afx --daemon ops web enable` へ書き換わる。書換え後の扱いは、現行の `afx --daemon ops enable web` と同じである。

`ops` の旧形と `reconf` も同じ argv shim で扱う。以下の「先頭語」「2 語目」「3 語目」は head からの位置を指す。先頭語が `ops` で 2 語目が `enable` / `disable` / `status` なら旧形である。3 語目が `bot` / `web` / `notify` なら 2 語目と 3 語目を入れ替え、案内を stderr に出してから通常の parse へ進む。3 語目が無い、または不正なら案内だけを出して rc=2 で終える。先頭語が `ops` で 2 語目が `web` かつ 3 語目が `reconf` のときだけ `reconfigure` へ書き換え、案内は出さない。bot / notify の `reconf` は書き換えず、打った綴りのまま argparse の usage error (rc=2) にする。argparse には新形の parser だけを登録する。このため旧形と `reconf` は choices にも通常 help の一覧にも現れない。`reconf` は `afx ops --help` の `reconfigure` 行の説明文 `(別名 reconf)` にだけ書く。

`improve` の廃止 compatibility record と `ops` 旧形の期限つき互換別名 record は、どちらも `deprecated_since="0.2.0"`、`remove_in="0.3.0"` を持つ。「1 版だけ残し、次版で削除」という裁定を、どちらについても次の minor release と読む。対話 shell の `1.0.0` は §8.2-1 の別件であり、この 2 record とは関係しない。version の比較は `.` 区切りの各成分を整数にした tuple の辞書順比較とし、`packaging` には依存しない。実行中 package version が `remove_in` 以上なら record が残っている test を失敗させ、期限超過の shim を自動検出する。

`deprecated_since="0.2.0"` を事実にするため、I1 で `pyproject.toml` の `version` を `0.1.0` から `0.2.0` へ上げる (`pyproject.toml:3`)。`importlib.metadata.version("agentic-fx")` が各 record の `deprecated_since` 以上であることも test で pin する。

互換別名は処理を継続するため別の API endpoint を作らない。別名そのものは分類表と通常 help に重複掲載せず、「互換名」節だけに載せる。

### 2.3 対話 shell

対話 shell はこの版で廃止しない。daemon 内から `ShellCaller.SHELL` として同じ ops 関数を呼ぶ既存の互換 UI と位置づける (`ops/contracts.py:76-83`)。第 3 のレーンとは呼ばない。本 release で deprecated 表示を help 末尾の 1 行に追加し、次 major (`1.0.0`) で削除する案を既定とする。最終裁定は §8.2-1 に残す。下の固定文の `1.0.0 で削除予定` はこの裁定に依存するので、§8.2-1 の裁定は I1 の着手前に取る。

`commands.py:28-47` の手書き `_HELP` は削除する。分類メタデータのうち `shell=True` の操作から help を生成する。shell に実装されていない `events`、`jobs`、`whoami` は生成対象にしない。`improve add` は `backlog add` を正式表示にし、旧形を互換別名として受理する。

`stop` は `classified=False, shell=True, label="change"` の互換 record として同じ metadata module に置く。7 群の表や `afx ctl help` には出さないが、shell help には `stop  daemon を graceful shutdown する` と表示する。dispatch は現行の shutdown 要求経路を維持し、API の「サービス停止は無い」と混同させない。help 末尾に `廃止予定: 対話 shell は 0.2.0 で deprecated。1.0.0 で削除予定。afx ctl を使用してください。` の 1 行を出す。

## 3. 分類メタデータと help 生成

### 3.1 正本と依存方向

新規 `src/agentic_fx/cli_surface.py` は純データ module とし、`agentic_fx.ops` package の外に置く。import は stdlib だけに限る。`agentic_fx` の他 module、`.env`、setup 永続層、service、DB、httpx を import しない。

`ops` package の中に置かない理由は、package の読込が service と DB 層を連れてくるからである。`ops/__init__.py:7` が `from .service import OpsService` を即時 import するので、`import agentic_fx.ops.contracts` だけでも `agentic_fx.ops.service` と `agentic_fx.store.db` が `sys.modules` に入る。`ENDPOINTS` も contracts ではなく `ops/service.py:74` にある。

このため metadata の `scope` と `endpoint_code` は文字列で持つ。`Scope` / `ENDPOINTS` との照合は production import ではなく test だけで行う。制約は「新しい子 process で `import agentic_fx.cli_surface` した後、`sys.modules` にある `agentic_fx` 配下の module が `agentic_fx` と `agentic_fx.cli_surface` の 2 つだけ」という test で pin する。

`ops/client.py` については、setup 永続層の 3 module (`agentic_fx.setup.checks`、`agentic_fx.store.setup_record`、`agentic_fx.setup.pending_steps`) を load しない既存 pin だけを維持する (`tests/ops/test_ctl_pins.py:639-645`)。この pin は「client が service / setup を一切 import しない」ことを意味しない。client は現在も `ops` package 経由で service を読み込み、`agentic_fx.setup.pending_text` を直接 import する (`ops/client.py:36`)。本束はこの依存を変えない。本束の import 制約は `cli_surface.py` 自身の import 閉包に限る。

各 command record は次を持つ。

| field | 内容 |
|---|---|
| `group`, `group_order` | 7 群の enum と固定順 |
| `lane` | `local` / `ctl` |
| `command`, `usage`, `shell_usage` | `usage` は常に `afx ctl status` のような完全形の正式表示文字列。shell に出す裸 token は別 field の `shell_usage` に置く |
| `label` | `read` / `change`。`confirm` と `approver` は独立 bool |
| `scope` | `Scope` の値と同じ文字列 (`"status.read"` 等) または `None`。enum は import しない |
| `endpoint_code` | `ENDPOINTS` の key と同じ文字列または `None`。照合は test だけで行う |
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

`implemented` は record ごとに持つ。`ops <feature> reconfigure` は 3 機能分の record を持ち、値を次で固定する。web は `implemented=True, exposed=True` である。§0.1 の web-lan spec が動作本体 `run_ops_reconfigure(root, feature) -> int` を作るのは web だけなので、bot と notify は `implemented=False, exposed=False` である。web の値は web-lan の本体が main に入っていることを前提とし、I1 はその merge 後に着手する (§10.2)。`implemented=False` の機能の `reconfigure` は parser に登録せず、help にも出さない。

`entry.py` と `backtest/cli.py` は argparse の parser 群を従来どおり登録する。分類メタデータは parser の実装 (引数定義と dispatch) を置き換えない。ただし利用者向けの help 文言は、すべて `cli_surface.py` の accessor から受け取る。対象は次のとおりである。

| 文言 | 渡し方 |
|---|---|
| 最上位 `ArgumentParser` の `description` と `epilog` | `top_description()`、`render_top_epilog()` |
| 各 `add_parser()` の `help=` (親 command の一覧行) | `parser_help(command)`。ラベルと summary を連結した 1 行 |
| 各 `add_parser()` の `description=` | `parser_description(command)`。値が `None` の parser は description を出さない |
| 葉操作の一覧行 (`afx ops web --help` の `enable` 等) | `leaf_help(command)`。ラベル、summary、condition を連結した 1 行 |
| 親 parser の `epilog` (`afx ops --help` の操作表等) | `render_epilog(command)` |
| `afx ctl help` | `render_ctl_help()`。`client.py` の静的 `USAGE` を置き換える |
| 対話 shell の help | `render_shell_help()`。`commands.py` の `_HELP` を置き換える |

`epilog` は `RawDescriptionHelpFormatter` で改行を固定する。option (`add_argument`) の `help=` は引数定義と一体なので metadata へ移さず、各 parser の定義に残す。parser とメタデータの全件一致を test で保証する。

正本が 1 つであることも test で検査する。`entry.py`、`backtest/cli.py`、`ops/client.py`、`commands.py` の AST を読み、(1) `ArgumentParser()` と `add_parser()` の `help` / `description` / `epilog` keyword に str literal (f-string を含む) が無いこと、(2) metadata の各 record の `summary`、`condition`、群名、および空白を含む `usage` が、この 4 file の str 定数のどれとも完全一致しないこと、を確かめる。`usage` は常に完全形なので空白を含む。shell の裸 token は `shell_usage` に分け、検査 (2) の対象外とする。`prog` と option の `help=` は対象外である。全文 golden は出力を固定するが、同じ文言が 2 か所に書かれることは防がないので、この検査を別に置く。

### 3.2 `afx --help` の全文 golden

formatter の幅を 100 に固定し、stdout 全体を次の byte 列と一致させる。本節以降のすべての help golden test は、実行前に `FORCE_COLOR`、`NO_COLOR`、`PYTHON_COLORS`、`TERM` など色に関わる環境変数を消して実行する。golden は TTY に出さず、`parser.format_help()` の戻り値 (stdout への書き出しを伴わない) と、`--help` 実行は pytest の capture (非 TTY の pipe) で取る。Python 3.13 の argparse は `color` 引数を受けないため、`sys.version_info >= (3, 14)` のときだけ `color=False` を渡し、3.13 では渡さない。test はこの 2 条件 (非 TTY と条件付き `color=False`) の両方を満たした上で byte 一致を要求する。左レーンの親 command にもラベルを付け、副作用が下位操作ごとに異なる場合は `[下位操作ごと]` とする。ラベルは positional 一覧にだけ付け、末尾の「用途」表には左右どちらの列にも付けない。各 `afx <sub> --help` では葉操作ごとに `[読取]` / `[変更]` / `[確認]` と condition を出す。全角を 2 桁、`—` を 1 桁で数えて、どの行も 100 桁以内に収める。

```text
usage: afx [-h] [--daemon] {init,check,keys,ops,ctl,status,history,backtest,analyze,plugin} ...

agentic-fx

positional arguments:
  {init,check,keys,ops,ctl,status,history,backtest,analyze,plugin}
    init                [変更] 初期設定 (対話。`--non-interactive` で従来の非対話)
    check               [下位操作ごと] 設定と実行環境の検査 (`backend` は非隔離・課金ありの接続確認)
    keys                [変更] 操作 API の鍵管理 (停止中のみ)
    ops                 [下位操作ごと] bot / web / notify の有効化・無効化・再設定・状態表示
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

用途                     daemon 不要                稼働 daemon
導入・接続管理           init / check / keys / ops  ctl whoami
稼働観測・実行追跡       status                     ctl status / log / activity / events / jobs
市場入力を整える         history                    —
調査・評価する           backtest / analyze         —
問い合わせ・改善運用     —                          ctl ask / improve / backlog / policy / reflect
Plugin を採否・配備する  plugin                     ctl approval / approve / reject
保護状態を回復する       —                          ctl killswitch / data resume

ラベル: [読取] 状態を変えない  [変更] 状態を変える  [確認] 端末確認  [approver] approver 権限
詳しい操作: afx <sub> --help / afx ctl help
```

### 3.2.1 `afx ops --help` と `afx ops <feature> --help` の全文 golden

`afx ops --help` の stdout 全体を次の byte 列と一致させる。機能 3 つを positional にし、動詞は epilog の固定表で示す。

```text
usage: afx ops [-h] {bot,web,notify} ...

bot / web / notify の有効化・無効化・再設定・状態表示

positional arguments:
  {bot,web,notify}
    bot             操作 bot (ops-bot)
    web             web 表示 (ops-web)
    notify          通知 (ops-notify)

options:
  -h, --help        show this help message and exit

操作:
  afx ops <feature> enable        [変更] 有効化する (必要時だけ daemon を一時停止して再開)
  afx ops <feature> disable       [変更] daemon を止めずに無効化する
  afx ops <feature> status        [読取] durable stage を表示する
  afx ops web reconfigure         [変更] afx に触れずに設定を変える (別名 reconf)

旧形 `afx ops enable|disable|status <feature>` は 0.2.0 だけ受け付け、0.3.0 で削除する。
```

`afx ops web --help` の stdout 全体を次の byte 列と一致させる。

```text
usage: afx ops web [-h] {enable,disable,reconfigure,status} ...

positional arguments:
  {enable,disable,reconfigure,status}
    enable              [変更] 有効化する (必要時だけ daemon を一時停止して再開)
    disable             [変更] daemon を止めずに無効化する
    reconfigure         [変更] afx に触れずに設定を変える (別名 reconf)
    status              [読取] durable stage を表示する

options:
  -h, --help            show this help message and exit
```

`afx ops bot --help` の stdout 全体を次の byte 列と一致させる。bot は `reconfigure` が `implemented=False` なので、choices にも一覧にも出さない。

```text
usage: afx ops bot [-h] {enable,disable,status} ...

positional arguments:
  {enable,disable,status}
    enable              [変更] 有効化する (必要時だけ daemon を一時停止して再開)
    disable             [変更] daemon を止めずに無効化する
    status              [読取] durable stage を表示する

options:
  -h, --help            show this help message and exit
```

`afx ops notify --help` は 1 行目が `usage: afx ops notify [-h] {enable,disable,status} ...` である以外、bot と同じ byte 列とする。

この 4 つの golden は本 release の固定値であり、I1 の実装状態で変えない。§3.1 の `implemented` / `exposed` の値と一体で固定する。bot / notify に `reconfigure` の本体を足すときは、record と golden を同じ変更で改訂する。

### 3.2.2 help に出す条件

`news` は `planned=True, implemented=False, exposed=False` の予約 record とし、本 release の choices と help には出さない。同様に `backlog list` / `policy show` / `policy remove` / `policy reconcile` は B の parser / endpoint と test が完了するまで `exposed=False` とし、最終統合 task でだけ true にする。help は実装済みの利用可能性を表す契約である。

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
  [変更] policy reconcile
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

- `cli_surface.py` の正式な local 親 command と argparse の全 `add_parser` を一致させる。`ops` は機能 → 動詞の 2 段まで比較する。予約行 `news` と `ctl` ダミー parser は明示的な例外集合にする。
- ctl の正式名と互換別名を parser table 化するか、独立 fixture から `parse()` へ全語順を与える。各正式名は 1 record にだけ対応させる。
- API command の `endpoint_code`、scope、mutating の文字列を `ENDPOINTS` と比較する。この surface ↔ `ENDPOINTS` 照合 (AC-4) は I1 で有効化し、schema task 単独の完了条件にはしない。
- `[読取]` は `ENDPOINTS[code].mutating is False` と一致させる。`[変更]` は `True` と一致させる。
- `[approver]` は `_APPROVER_OPS` と一致させる。`[確認]` は `_CONFIRM_OPS` と一致させる。`jobs cancel` の差を pin する。
- `cli_surface.py` の `scope` 文字列は `ENDPOINTS[code].scope.value` と完全一致させる。
- endpoint は 4 本増えて合計 29 になる。production の個数 pin `_ENDPOINT_COUNT` (`ops/api_server.py:274-281`) 、test の個数 pin (`tests/ops/test_ac01_02_43_api_routing.py:91,282`)、独立表 `_EXPECTED_TABLE` (同 `:251-288`) は、endpoint を足す task が自分で更新する。B1 で `backlog_list` を足して 26、B2b で `policy_show` / `policy_remove` / `policy_reconcile` を足して 29 にする。pin を後回しにすると `build_routes()` が起動時に `RuntimeError` を送出し、listener が開かない。
- `approval_change` は CLI の詳細表示が内部で使う endpoint だが、独立コマンドではない。分類漏れとは扱わない。

## 4. レーン規則に合わないものの裁定

### 4.1 `keys` と `ops <feature> enable`

どちらも最上位レーンに残すが、停止契約は分ける。`keys` だけは `condition="停止中のみ"` とし、daemon 稼働を検知したら無変更で rc=1 を返す。

`ops <feature> enable` は現行の durable 状態機械を保つ。語順の変更は parser と argv shim だけで、`interactive_enable(root, feature)` / `run_ops_command(root, verb, feature)` への dispatch は変えない (`entry.py:81-86`)。すなわち、marker 作成 → daemon 停止と停止確認 → instance lock 取得 → enroll / install → lock 解放 → 元が稼働中なら daemon 再起動 → marker 除去の順である。metadata の condition は「必要なときだけ一時停止して再開」とし、稼働中を rc=1 で拒否しない。crash 後の再開と元が停止中のときは起動しない既存契約も維持する。これにより §0.1 の「daemon の挙動は変えない」と整合する。

### 4.2 `verify-backend`

`afx check` の引数なし形はローカル読み取り専用のままにする。ネットワーク往復とサブスク枠を使う検査は `afx check backend` という明示サブコマンドへ分離し、`[変更]` と表示する。

親 `check` の `--json` / `--unit-env` (`entry.py:37-40`) は `backend` と組み合わせられない。`afx check --json backend …` のように併用したら、backend を呼ぶ前に stderr へ ``--json` / `--unit-env` は `afx check backend` と併用できません。` を出して rc=2 で終える。黙って無視しない。

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

別名と廃止名は §2.2 の 2 種に分ける。廃止名と期限つき互換別名は parser の通常 command record へ入れず、argv shim 用の期限つき compatibility record にする。`deprecated_since` と `remove_in` (`improve` も `ops` 旧形も `0.3.0`) を record に持たせ、期限 pin で検査する。

ctl の互換別名は `parse()` の戻り値 `Op` に `via_alias` (旧名の文字列、正式名なら `None`) を持たせる。client はこの印を見て旧名のときだけ stderr に案内を出す。`via_alias` は表示専用であり、認可、idempotency payload、API への送信内容には入れない。

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

1 行は `(name, kind)` 1 組とする。`version` は immutable な content hash (表示は一意な最短 12 文字以上の prefix、衝突時は full hash) である。下の固定例も 12 文字の prefix で書く。同じ name の indicator と strategy は別行にし (pending の kind が live と違えば issue `kind_mismatch` を付けた別行、kind が欠けているか不正なら live が単一 kind のときその行に同居させ issue `kind_missing` を付ける)、name 昇順、kind は `indicator` → `strategy` → `unknown` の順とする。同じ source 内の重複 name / version は上書きせずすべて保存し、version → approval id / operation id の順に並べ、`issues` に `duplicate_live`、`duplicate_pending` または `duplicate_open_op` を追加する。manifest を読めない plugin も落とさず `kind=unknown`、`issues=[broken:<fixed-reason>]` で出す。`--kind` 指定時は指定 kind の行だけを出し、unknown 行は出さないが source status は常に出す。終了コードは表示した行から算出し、`--kind` で隠した行の issue は数えない。

stdout は source status 3 行と固定列 `NAME KIND LIVE PENDING OPEN_OP ISSUES` からなる。各列は見出しと値の最大幅まで空白で埋め、列の間は空白 2 つ、行末の空白は削る。配列は `,` で結合し、空は `-` とする。live `3f9a1c2b7d4e` + pending `8b0e6d5a2c91` + 未完 rollback を同時に持つ固定例は次とする。

```text
source live: ok
source pending: ok
source journal: ok
NAME      KIND       LIVE          PENDING       OPEN_OP                   ISSUES
momentum  strategy   3f9a1c2b7d4e  8b0e6d5a2c91  17:rollback:3f9a1c2b7d4e  -
rsi       indicator  -             -             -                         broken:invalid_manifest
```

live directory 不在は `source live: missing`、DB 不在は pending / journal の両方を `missing`、table 不足・破損・permission error は該当 source だけ `unavailable` とする。missing は rc=0、broken item または unavailable が 1 つでもあれば残りを表示して rc=1 とする。どの経路も DB、directory、lock file を作らない。DB は §6.4 の観測用接続だけで開く。主 DB file は byte 一致を保ち、変化してよいのは §6.4 が名指す WAL sidecar 2 file だけである。

## 5. 新設 API

### 5.1 endpoint

| code | route | scope | mutating | idempotent | autopilot 制限 |
|---|---|---|---|---|---|
| `backlog_list` | `GET /v1/backlog` | `guidance.read` | false | — | — |
| `policy_show` | `GET /v1/policy` | `guidance.read` | false | — | — |
| `policy_remove` | `POST /v1/policy/{id}/remove` | `operate` | true | true | true |
| `policy_reconcile` | `POST /v1/policy/reconcile` | `operate` | true | true | false |

`api_server.py` は現在 GET / POST だけを受ける (`api_server.py:283-286`)。remove と reconcile も既存の idempotency と監査の枠へ載せるため POST とする。endpoint 総数は B1 で 26、B2b で 29 になる (§3.4)。`policy_reconcile` の `autopilot_restricted` は false とする。DB を変えず投影を DB 正本へ合わせるだけであり、autopilot 中にも修復経路を残すためである。

読取は新 scope `guidance.read` とする。backlog の idea / last_result と policy directive は利用者の自由文であり、`logs.read` の流用はしない。`PRINCIPAL_SCOPES` は operator / approver にだけ `guidance.read` を追加し、ops-web / ops-bot / ops-notify には与えない。endpoint ごとの特例 deny ではなく scope 照合で一貫して拒否する。`tests/ops/test_scope_enforcement.py::test_scope_table_matches_the_spec`、`::test_every_endpoint_admits_exactly_the_principals_holding_its_scope`、`tests/ops/test_ac56_57_ops_principals.py::test_ac56_authorization_table_matches_the_spec_for_every_principal`、`::test_ac56_every_route_is_allowed_only_by_the_spec_scope` の principal × scope pin を更新する。bot、web、notify の UI / route も追加しない。

### 5.2 `backlog list`

既定は活動中の `open`、`observation`、`note`、`selected` を返す。`--all` は status で絞らず、全行を返す。`improvement_backlog.status` には CHECK 制約が無いので (`store/backlog.py:7-8`)、`done` / `rejected` 以外の未知の値も `--all` では見える。並びは `updated_at DESC, id DESC` とする。既定上限は 100、`--limit` の許容範囲は 1〜500 とする。

応答は次の固定 shape とする。

```json
{"items":[{"id":12,"status":"open","source":"user","updated_at":"2026-10-23T00:00:00+00:00","idea":"...","idea_truncated":false,"last_result":null,"last_result_truncated":false}],"limit":100,"has_more":false}
```

API 応答の `idea` と `last_result` は redactor と改行 / 制御文字の正規化後にそれぞれ UTF-8 4,096 byte を上限とする。超過時は文字境界で切り、上限内の末尾を固定印 `…[truncated]` にし、対応する `idea_truncated` / `last_result_truncated` を true にする。`null` は切り詰めず false である。これにより 500 件の最大応答も有界にする。CLI は 1 行 1 件で `#<id> <status> <source> <updated_at> <idea>` を出す。`last_result` があるときだけ次行へ 2 空白 indent で出す。0 件は `backlog: 0 件` と出す。CLI 表示は API の制限後の文をさらに 200 文字へ畳む。応答の `has_more` が true のときは、最後の行の後に固定行 `… 他にもあります (--limit N で増やすか --all)` を stdout へ 1 行出す。false のときは出さない。`last_result` が `null` または空文字のときは次行を出さない。API 側の redactor は 8 KiB 超の値を全文伏せるので、redactor へ渡す前に UTF-8 8,192 byte 以内へ文字境界で前段切り詰めし、切った末尾は最後の空白まで戻す。ここでの空白は redactor の制御文字除去 (`strip_controls`) の後も残る空白 (空白・タブ・改行など) に限り、redactor が除く CR / VT / FF / 情報分離子などは token の中を繋ぐ字として区切りに数えない。そのような空白が 1 つも無い値は token の途中かを判別できないので本文を残さず固定印だけにする。前段で切った値は `*_truncated` を true にし、本切り詰めと同じ固定印を付ける。

store に `list_items(conn, *, all_statuses: bool, limit: int) -> tuple[list[dict], bool]` を足す。`all_statuses=False` は上の 4 値の `IN`、`True` は status 条件なしである。SQL は `limit + 1` 件を読み、`has_more` を決める。既存 `list_open` と `list_notes` の意味は変えない (`store/backlog.py:57-65`)。

### 5.3 `policy show`

`ops_policies.id` を削除キーとする。DB を唯一の正本とし、`policy/directives.md` は再生成可能な投影とする。file の行番号や directive 文面は識別子にしない。

管理行は次の ID 付きコメント block だけで識別する。ID は 10 進の `ops_policies.id`、directive は対応 DB row の `_render_directive()` 結果である。

```markdown
<!-- afx:managed-policy id=3 -->
- 週末の新規発注を禁止する
<!-- /afx:managed-policy id=3 -->
```

#### 5.3.1 marker の認識と投影

file は `\n` だけで行へ分割する。各行の照合用 view では末尾の `\r` を高々 1 つだけ外すが、外側と unmanaged の保存は元の byte 列のままとする。marker はこの照合用 view の行全体の一致でだけ認識する。開始は `^<!-- afx:managed-policy id=([1-9][0-9]*) -->$`、終了は `^<!-- /afx:managed-policy id=([1-9][0-9]*) -->$` である。開始行、directive 1 行、同じ ID の終了行の連続 3 行だけが managed block である。したがって CRLF file の正しい block も認識・照合できる。

directive 行は `_render_directive()` (`ops/service.py:231-232`) の結果であり、空白と改行を 1 つの空白へつぶして必ず `- ` で始まる。このため directive 本文が marker 行になることはない。`policy add "x <!-- /afx:managed-policy id=3 -->"` のような文面でも block の境界は壊れない。

marker 外の手書き行、旧 shell の legacy 行、対にならない marker、ID が食い違う marker は byte 列のまま保持し、unmanaged かつ `removable=false` と表示する。開始または終了 marker の正規表現に一致するが認識済み managed block に属さない行が 1 つでもあれば、その file は `malformed` の stale である。この marker は所有者を確定できないため、reconcile は削除も replace も行わない。移行 (§5.3.2) の 1 回を除き、DB と同じ文面が marker 外にあっても管理行に吸収しない。同じ ID の block が複数あれば全部を managed と認識する。下の照合で `duplicate` の stale になり、再生成で 1 つに戻る。

再生成は認識済み managed block を全て除いた外側の byte 列をそのまま保持する。外側が空でなく `\n` で終わらないときだけ `\n` を 1 つ足す。その後ろ、つまり file の末尾に、DB の `id ASC` で managed block を並べる。末尾に置くのは現行の再生成と同じである (`ops/service.py:725-737`)。書込みは同一 directory の temp file を fsync 後に replace する。

投影が DB 正本と合っているかは、ID の集合ではなく、出現数と順序を含む列の完全一致で判定する。集合では、同じ ID の block の重複も、block の本文だけの改変も検出できず、Mission に入る方針が DB と食い違ったまま残るからである。

- 期待列は、DB の `ops_policies` を `id ASC` で読んだ `[(id, _render_directive(directive))]` である。これを canonical 列と呼ぶ。
- 実際の列は、file の managed block を file 内の出現順に読んだ `[(id, directive 行)]` である。
- 2 つの列が長さ、各要素、順序まで一致するときだけ `ok` とする。それ以外は `stale` とする。

stale の理由は次の固定語 8 つで返す。当てはまるものを全て、この表の順で返す。ただし `unreadable` は file 内容を安全に観測できないため単独で返し、`unmanaged=[]` とする。

| 理由 | 条件 |
|---|---|
| `migration_pending` | `data_migrations` に `policy_directives_managed_v1` が無い |
| `unreadable` | file が存在するが読めない。file 不在は空 file として照合する |
| `malformed` | marker の正規表現に一致するが、認識済み managed block に属さない行がある |
| `missing` | DB の id の block が file に無い |
| `orphan` | file の block の id が DB に無い |
| `duplicate` | 同じ id の block が file に 2 つ以上ある |
| `content` | 同じ id の block の directive 行が canonical 列の値と違う |
| `order` | `missing` / `orphan` / `duplicate` / `content` のどれにも当たらないが、block の並びが `id ASC` でない |

managed block と marker 外の行の相対位置は照合に含めない。再生成は block を末尾へ集めるが、手書き行が block の後ろへ足されただけでは stale にしない。

投影の規則は新規 `src/agentic_fx/policy_projection.py` に置き、service、初回設定、shell が共有する。この module は `_render_directive()` を service から移して持ち、次の 4 つを提供する。

| 関数 | 役割 |
|---|---|
| `insert_policy(conn, text, *, request_id, now) -> int` | `ops_policies` へ 1 行 insert して commit し、整数 `id` を返す |
| `regenerate_policy_file(conn, path) -> None` | DB 全行から上の規則で file を書き直す |
| `inspect_policy_file(conn, path) -> ProjectionStatus` | DB と file を読んで上の照合を行い、`ProjectionStatus(status: "ok" \| "stale", reasons: tuple[str, ...])` を返す。file、DB、`data_migrations` のどれにも書かない |
| `reconcile_policy_file(conn, path) -> bool` | §5.3.2 の移行を先に行い、その後 `inspect_policy_file()` が `unreadable` / `malformed` 以外の `stale` のときだけ再生成する。これら 2 理由では専用例外で停止し、file を replace しない。`changed` は replace 前後の byte 列が異なるときだけ true とし、同一 byte の書き直しはしない。`ok` なら file に触れず false を返す |

`ops_policies.request_id` は `NOT NULL UNIQUE` である (`store/db.py:509-514`)。API と shell は既存どおり accepted request の id を入れる。初回設定は `uuid4().hex` を入れる。`policy add` の応答 `{"id": <request_id>}` は変えない (`ops/service.py:715`)。削除キーの整数 `id` は `policy show` で知る。

#### 5.3.2 旧形式 file の 1 回限りの移行

v0.1 までの再生成は marker なしの `- <directive>` 行を書いた (`ops/service.py:725-737`)。この旧形式をそのまま新規則で読むと、同じ文面が marker 外と managed block の両方に入り、Mission へ 2 回注入される。`policy remove` も marker 外の行を消せない。これを避けるため、次の移行を 1 回だけ行う。

0. file が `unreadable`、または `malformed` marker を含むときは移行を始めず、専用例外で停止する。file 内容と migration record は変えない。
1. `data_migrations` に `policy_directives_managed_v1` があれば何もしない。2 回目以降は ID marker だけで判定する。
2. file に managed block が 1 つでもあれば、行の採用はせず `data_migrations` に記録だけする。
3. managed block が 1 つも無いとき (= 旧形式、または file 不在) だけ、`\n` で分割し、照合時だけ末尾の `\r` を 1 つ外して、DB 全行の `_render_directive()` の集合を作る。この集合に一致する marker 外の行は、何行あっても全て legacy-owned として外側から除く。現行 `_regenerate_policy()` が `owned = set(rendered)` で除外している行と同じである (`ops/service.py:725-737`)。
4. 外側の残りを byte 保存し、DB の行ごとに 1 つずつ (`id ASC`) managed block を末尾に並べた候補 byte 列を作る。一致する行が無かった DB 行も block にする。旧規則の再生成もそれらを書いていたので、注入内容は変わらない。file 不在かつ DB 0 行では file / directory を作らず、候補を書かない。既存 file でも候補 byte 列が同じなら replace しない。
5. file の replace が済んでから、`data_migrations(name, applied_at)` を insert して commit する。

file を書いた後で記録の前に crash しても、次回は手順 2 に当たり、記録だけして終わる。file 不在かつ DB 0 行では記録だけを残し、file / directory は作らない。DB が 0 行で既存 file も変わらない場合も、replace せず記録だけを残す。旧規則も集合に一致する行を全て除き、DB 行ごとに 1 行を書き直していた (`ops/service.py:730-737`)。このため移行後に注入される directive の種類と回数 (DB の行数と同じ) は、旧規則の次の再生成と同じである。たとえば DB に A が 1 行、旧 file に `- A` が 2 行あれば、移行後の A は block 1 つだけになり、注入は 1 回である。DB に B が 2 行、旧 file に `- B` が 1 行あれば、block は 2 つで、注入は 2 回である。

DB 行の無い旧 shell / 旧初回設定の行 (`commands.py:49-54` の `\n- text\n`) は採用せず、unmanaged で残る。

#### 5.3.3 Mission への注入

`Policy.tail()` (`policy.py:16-22`) は file 全体を `\n` で分割し、§5.3.1 と同じ block 認識 (照合時だけ末尾の `\r` を 1 つ外し、開始 / directive / 同 ID 終了の連続 3 行) で**認識済み managed block の marker 行だけ**を除き、他の行 (malformed な marker 行を含む unmanaged 行) と各行の行終端は元のまま保って、その結果の末尾 4000 文字を返す。marker 除去は `policy_projection` の公開 helper 1 つで行い、service / policy が同じ規則を共有する。directive 行は残る。`Policy.size_warning()` も同じ marker 除去後の文字数で判定する。marker は注入予算を消費せず、切り口に marker の断片が来ることもない。

#### 5.3.4 初回設定と shell が書く方針文

初回設定の段 12 は、方針文を `ops_policies` に insert して managed にする (`setup/wizard.py:2016-2050`)。段 12 は段 0 で取った instance lock を保持したまま (`setup/wizard.py:662`)、段 10 の DB 初期化 (`setup/wizard.py:1910-1913`) の後に走る。この順序は現行のままである。変わるのは書込み先で、`append_policy_directive` の代わりに `insert_policy` → `reconcile_policy_file` を呼び、activity の `policy_added` は従来どおり残す。既存の重複判定 (file 本文の部分一致) は維持する。

段 12 の失敗表示は次の 2 つに分ける。insert 前の失敗は従来の `警告: 方針文を書けませんでした (<理由>)。` である。insert の commit 後に file を書けなかったときは `警告: 方針文は記録しましたが policy/directives.md へ反映できませんでした (<理由>)。afx の次回起動時に反映されます。` と出す。どちらも段 12 をそこで終え、書いた所までが残る。

shell の `policy add` は `ops_service` がある通常経路では既存どおり `add_policy` を呼ぶ (`commands.py:414-416`)。`ops_service is None` の fallback (`commands.py:417-420`) も `append_policy_directive` をやめ、`self.conn` に対して同じ `insert_policy` → `reconcile_policy_file` を呼ぶ。`append_policy_directive` は呼び出し元が無くなるので削除する。 shell fallback の失敗表示も段 12 と同じ 2 分割とし、insert 前の失敗は `policy を書けませんでした (<理由>)`、commit 後の file 反映失敗は `policy は記録しましたが policy/directives.md へ反映できませんでした (<理由>)。afx の次回起動時に反映されます。` を返す。成功時の `policy に追記しました` と activity の `policy_added` (先頭 200 字) は変えない。

段 12 と shell fallback は同じ書込み手順を共有する。順序は API の `add_policy` (§5.4 末尾) と同じく、(1) 先頭で `reconcile_policy_file` (投影の不整合をここで直す。unreadable / malformed はここで止まり、行を足さない)、(2) 本文の検査 (空でない、4000 字以下。API と同じ上限)、(3) `insert_policy` (INSERT + commit、`request_id` は `uuid4().hex`。`ops_policies.request_id` は `NOT NULL UNIQUE` で、`ops_requests` への外部キーではない)、(4) `reconcile_policy_file` で file へ反映、(5) activity `policy_added` の順である。(1)〜(3) の失敗は「insert 前の失敗」、(4) の失敗は「commit 後の file 反映失敗」として上の 2 文に写す。(5) は `ActivityLog.write` の「送出しない」契約 (失敗は warning ログに残る) に従い、呼び出し側は成功扱いで続ける。file は反映済みなので第 2 文にはしない。もし将来 activity が例外を出す実装になっても helper は包まず、段 12 は `警告: 方針文を書けませんでした (<理由>)。` に写す。段 12 の重複判定 (file 読取と DB 照合) は helper の先頭 reconcile より前に走るので、ここでの `OSError` (file が directory、権限、非 UTF-8) と `sqlite3.Error` も第 1 文に写して段を終える。`insert_policy` は commit が失敗したら rollback してから例外を出し、長命の接続に未確定の INSERT を残さない。

段 12 の重複判定は file 本文の部分一致に加えて `ops_policies.directive` の完全一致も見る (commit 後の file 反映失敗の直後に同じ文を入れ直しても行を二重にしない)。段 12 は DB 接続を段の先頭で 1 回開き、方針文と最初の課題の両方で使う。接続を開けないときは `警告: データベースを開けませんでした (<理由>)。方針文と最初の課題はあとから afx ctl policy add / afx ctl backlog add で足せます。` と出して段 12 を終える (他の段は続く)。

#### 5.3.5 `policy show` の応答と表示

`policy show` は純粋な読取である。file、DB (`ops_policies`、`data_migrations`、監査 table)、activity のどれにも書かず、移行も reconcile も行わない。投影 file は Mission の挙動を変える実効状態なので、`GET` の副作用として書き換えない。投影の状態は `inspect_policy_file()` の結果を `projection` に入れて返すだけである。直すのは §5.4.1 の経路である。

`policy show` の応答は次の固定 shape とする。

```json
{"managed":[{"id":3,"created_at":"2026-10-23T00:00:00+00:00","directive":"..."}],"unmanaged":[{"line":1,"directive":"...","removable":false}],"limit":200,"has_more":false,"projection":{"status":"ok","reasons":[]}}
```

`managed` は DB の `ops_policies` 行である。stale でも file の block ではなく DB の行を出す。`unmanaged` は現在の file の marker 外の手書き / legacy 行と不正 marker であり、常に `removable=false` と表示する。 行の `directive` は照合用 view (末尾の `\r` を高々 1 つ外した文) で返し、view が空の行 (空行) は数えず出さない。見出しや説明文などの非空行は方針文かどうかを判別しないので出す。ただし file が `unreadable` のときは内容を推測せず空配列とする。移行後の同文判定は行わず、同一文の managed block と手書き行を ID marker で区別する。これらは `policy remove` では削除できない。

上限 `POLICY_SHOW_LIMIT=200` は managed と unmanaged の合計へ掛ける。managed は `id ASC`、unmanaged は file 順である。managed を先に budget へ入れる。残りへ unmanaged を入れる。超過時は `has_more=true` とする。directive は 4000 字の保存上限を保ち、CLI 表示だけ 200 字へ畳む。 畳み方は backlog と同じで、200 字を超える文は先頭 199 字 + `…` の計 200 字にする (AC-14 の 199 / 200 / 201 は表示幅が 200 を超えないことを pin する)。

CLI は次の形を固定する。

```text
管理対象:
  #3 2026-10-23T00:00:00+00:00 <directive>
管理外 (API から削除できません):
  line 1 <directive>
投影: ok
```

片方が 0 件でも見出しと `  0 件` を出す。stale のときは最後の行を `投影: stale (<理由>)。Mission に入る方針が DB と一致していません。afx ctl policy reconcile で DB に合わせられます。` とする。ただし `malformed` を含むときは固定文を `投影: stale (<理由>)。marker を手修正してから afx ctl policy reconcile を実行してください。` とし、`unreadable` を含むときは `投影: stale (<理由>)。policy/directives.md を読める状態 (権限、UTF-8) に直してから afx ctl policy reconcile を実行してください。` とする (どちらも reconcile は `failed (internal)` になるため、自動修復を案内しない。両方含むときは `unreadable` の文を優先する)。`<理由>` は §5.3.1 の固定語を表の順に `, ` で結ぶ。stale でも rc は 0 である。

### 5.4 `policy remove <id>`

`id` は 1 以上の 10 進整数とする。`0`、負数、非数値 (`abc`、`1.5`、空文字) は client の parse で API 接続前に rc=2 とし、鍵も接続も使わない。存在する managed row だけを削除する。marker 外の手書き行と legacy shell 行は ID を持たないため削除できない。正の整数で DB に行が無い ID (削除済みを含む) は API が `not_found` を返す。

remove は既存の accepted → 終端の機構だけで書く。新しい journal phase は足さない。`ops_requests.phase` と `ops_idempotency.state` は `CHECK(... IN ('accepted','succeeded','failed','outcome_unknown'))` であり、`ops_requests` は UPDATE / DELETE を禁じる append-only trigger を持つ (`store/db.py:470-490`)。phase を増やすには table の作り直しと `schema_version` の migration が要るが、欲しい意味は既存の 4 値で表せる。

順序は次のとおりである。

1. `_mutate` が accepted を commit する (`ops/service.py:447-473`)。
2. action の先頭で `reconcile_policy_file()` を呼ぶ (§5.4.1)。
3. 対象 row が無ければ `not_found` で終える。
4. `DELETE FROM ops_policies WHERE id=?` を commit する。
5. `regenerate_policy_file()` で file を replace する。
6. 成功したら固定 activity `SYSTEM policy_removed id=<id>` を残し、応答 `{"id":<id>}` を返す。`_mutate` が `succeeded` の終端行を書く。

本書で「固定 activity」と呼ぶのは `policy_added` / `policy_removed` / `policy_reconciled` / `policy_reconcile_failed` の `policy_*` 行である。`_mutate` の終端が全 endpoint に共通して書く監査の投影行 `SYSTEM ops_<endpoint>` は本書の対象外で、変えない (`outcome_unknown` の終端にも従来どおり書く)。「activity を残さない」「activity 1 件」はいずれも `policy_*` 行についての規則である。

手順 4 の commit の後で手順 5 が例外になったら、成功とも失敗とも報告しない。action は専用の内部例外 `PolicyProjectionUnknown` を送出する。`_mutate` の except 節はこの例外だけを、code=`outcome_unknown`、state=`outcome_unknown` の終端行にして書き、`OpsError(OUTCOME_UNKNOWN)` を送出する。現行の `_terminal()` は state を渡さないので (`ops/service.py:412-423`)、`AuditStore.terminal()` の既存の `state` 引数 (`ops/audit.py:123-135`) へ通す keyword を足す。state を渡さないと phase は `failed` になる。activity はこの場合残さない。

投影層は、replace 後の migration record commit が不明になったとき `MigrationRecordUnknown`、`unreadable` / `malformed` で安全に reconcile できないとき `ProjectionReconcileBlocked` という独自例外を送出する。投影層自身は `outcome_unknown` を作らない。`policy_reconcile` endpoint だけが、`MigrationRecordUnknown` と、replace 後の応答用 `inspect_policy_file()` の例外を `PolicyProjectionUnknown` へ写し `outcome_unknown` にする。add / remove の action 先頭 reconcile はこれらを含む投影例外を既存の `failed` (`internal`) として終え、DB 変更へ進まない。

手順 1 の後、終端行の前に crash した要求は、起動時の既存 `recover_after_journal()` が `outcome_unknown` にする (`ops/service.py:1383-1404`)。投影の収束は §5.4.1 の起動時 reconcile と明示 reconcile が受け持つ。API の終端と投影の回復は分かれており、回復後も同じ key の結果を成功へ書き換えない。

`policy add` も同じ非原子性を持つ。現行は INSERT → commit → 再生成 → activity の順で (`ops/service.py:705-715`)、再生成の失敗は `failed (internal)` になる。DB には行が残るので、失敗と応答したのに次の再生成で policy が効く。add も commit 後の再生成失敗を `PolicyProjectionUnknown` で `outcome_unknown` にし、activity `policy_added` は成功時だけ残す。

add と同じ `operate`、idempotency key、autopilot 制限を使う (`ops/service.py:699-718`)。同じ key の再送は既存の replay で返す。成功時は保存済みの `{"id":<id>}`、不明時は回復後も `outcome_unknown` である (`ops/audit.py:90-104`、`ops/service.py:402-410`)。別 key で削除済み ID を送ると `not_found` を返す。

#### 5.4.1 投影の収束

`reconcile_policy_file()` は次の 3 か所で呼ぶ。`policy show` では呼ばない (§5.3.5)。

- 起動時。`_recover_ops` の `SELECT 1 FROM ops_policies LIMIT 1` のゲートを外し、DB の行数によらず呼ぶ (最上位の `src/agentic_fx/service.py:1954-1959`)。移行 (§5.3.2) を先に行い、その後で §5.3.1 の列を照合する。これで最後の 1 件を remove した直後の crash でも、DB 0 行に対して block が残り続けることがない。reconcile の例外は固定 activity `SYSTEM policy_reconcile_failed` と技術ログへ残すが、listener の起動を妨げない。致命として listener を開かないのは `recover_after_journal()` 自体の失敗だけである。
- 変更系 2 endpoint (`policy` add と `policy_remove`) の action の先頭。ops lane の中で呼ぶ。crash ではない replace 失敗の後に利用者が別 key で再実行したとき、`not_found` を返す前に投影が収束する。daemon の再起動を待たない。ここで `changed=true` なら、後続の DB 操作と独立した副作用として固定 activity `SYSTEM policy_reconciled` を残す。したがって投影を直した後に対象なしで `not_found` になっても、この activity は残る。
- 明示操作 `policy_reconcile` (§5.4.2)。

先頭の reconcile が例外になったら、DB を変える前なので既存どおり `failed` (`internal`) で終える。とくに `unreadable` / `malformed` は replace せず、この経路と明示 reconcile endpoint のいずれも `failed` (`internal`) で終える。

#### 5.4.2 `policy reconcile`

`afx ctl policy reconcile` は、投影 file を DB 正本へ今すぐ合わせる明示操作である。`policy show` が `stale` を示したとき、daemon の再起動や別の policy 変更を待たずに直すために使う。route は `POST /v1/policy/reconcile`、scope は `operate`、ラベルは `[変更]`、`mutating=true` である。add / remove と同じ idempotency key と `_mutate` の accepted → 終端で監査するが、§5.1 の理由により autopilot 制限はしない。引数は取らず、余分な引数は client が API 接続前に rc=2 とする。request body は `{}` である。

1. `_mutate` が accepted を commit する。
2. `reconcile_policy_file()` を呼ぶ。
3. 応答 `{"changed":<bool>,"projection":{"status":"ok","reasons":[]}}` を返す。`changed` は移行か再生成で replace 前後の byte 列が異なったときだけ true である。`projection` は reconcile 後の `inspect_policy_file()` の結果である。
4. `changed=true` のときだけ固定 activity `SYSTEM policy_reconciled` を残す。`_mutate` が `succeeded` の終端行を書く。

`ops_policies` は変えない。file の replace より前に失敗したら、file も DB も変わっていないので `failed` (`internal`) で終える。`unreadable` / `malformed` は replace せずこの扱いにする。移行で file を replace した後に `data_migrations` の commit が失敗したら、投影層の `MigrationRecordUnknown` を endpoint が `PolicyProjectionUnknown` へ写し、`outcome_unknown` にする。replace 後の応答用 `inspect_policy_file()` が例外になった場合も file は変わり得るため同じ `outcome_unknown` にする。次の reconcile は §5.3.2 の手順 2 に当たり、記録だけして終わる。同じ key の再送は既存の replay で保存済みの結果を返す。

CLI は成功時に `policy reconcile: changed` または `policy reconcile: unchanged` の 1 行を stdout に出す。

### 5.5 principal × scope

| principal | `backlog list` | `policy show` | `policy remove` | `policy reconcile` |
|---|---:|---:|---:|---:|
| operator | 許可 (`guidance.read`) | 許可 (`guidance.read`) | 許可 (`operate`) | 許可 (`operate`) |
| approver | 許可 | 許可 | 許可 | 許可 |
| ops-bot | 拒否 | 拒否 | 拒否 | 拒否 |
| ops-web | 拒否 | 拒否 | 拒否 | 拒否 |
| ops-notify | 拒否 | 拒否 | 拒否 | 拒否 |

## 6. オフライン `afx status`

### 6.1 正本の分離と instance lock の probe

`afx status` は停止中診断である。最初に instance lock を probe し、その結果で出力の形を決める。

probe は `data/instance.lock` を `O_RDONLY` で開く。`O_CREAT` は付けない。`flock(LOCK_SH|LOCK_NB)` を 1 回試し、取れたら直ちに `LOCK_UN` して close する。state、DB、log を読んでいる間は instance lock を保持しない。

保持しない理由は、同じ lock を `LOCK_EX|LOCK_NB` で取り、取れなければ即失敗する経路が複数あるからである (`store/instance_lock.py:23-41`)。daemon 起動 (最上位 `service.py:1169`)、`afx keys` (`ops/keys.py:569-573`)、`afx init` と `afx ops <feature> enable` (`ops_setup/setup_lease.py:94-102`)、post_start (`setup/post_start.py:131-133`) がそれである。診断の読取がこれらを拒否させてはならない。

probe の結果は次の 3 語で出す。

| 語 | 条件 |
|---|---|
| `instance: locked` | shared lock が `EWOULDBLOCK` で取れない。誰かが排他で保持している |
| `instance: free` | shared lock が取れた。取った直後に解放する |
| `instance: unknown` | file 不在、open / flock のその他の失敗 |

lock file や親 directory は作らない。判定前後の状態変化を含む TOCTOU はゼロと主張せず、「観測時点」固定文を locked / free / unknown のすべてで出す。

`instance: locked` のときは次の 5 行だけを出し、DB、state file、activity log の詳細を読まない。終了コードは 0 とする。

```text
agentic-fx offline status
instance: locked
locked-by: <setup|enroll:<feature>|enroll:<feature>+setup|unknown (daemon 稼働中とみられる)>
note: この表示は観測時点の情報です。複数データ源の原子的 snapshot ではありません。
next: <下表の 1 つ>
```

lock の保持者は daemon だけではない。初回設定、`afx keys`、`ops <feature> enable`、post_start も同じ lock を握る。このため `locked-by` は既存の marker から推定する。setup marker は `data/state/setup_in_progress` の存在 (`store/setup_marker.py:21-31`)、enroll marker は `read_marker()` の feature (`ops_setup/stages.py:128-150`) で見る。enroll marker が読めないときは `enroll:unknown` とする。両方あるときは enroll を先に書き、`next` も enroll 側にする。初回設定が両 marker を見たときに `afx ops enable` を先に案内する既存の規則 (`tests/setup/test_wizard_abort.py::test_both_markers_report_the_ops_enable_first`) と揃えるためである。どちらも読取だけで、作成や削除はしない。

| `locked-by` | `next` |
|---|---|
| `setup` | `afx init の完了を待ってから afx status` |
| `enroll:<feature>` または `enroll:<feature>+setup` | `afx ops <feature> status` |
| `enroll:unknown` または `enroll:unknown+setup` | `afx ops の有効化の完了を待ってから afx status` |
| `unknown (daemon 稼働中とみられる)`、operator または approver の鍵 file がある | `afx ctl status` |
| `unknown (daemon 稼働中とみられる)`、どちらの鍵 file も無い | `journalctl --user -u afx.service (operator の鍵が無いため afx ctl status は使えません)` |

鍵 file は `key_dir(root)` 配下の `<principal>.token` を lstat で存在だけ見る (`ops/keys.py:52-62`)。中身は読まない。鍵は `next` の表示を選ぶためだけに使い、locked / free / unknown の判定には使わない。marker の無い保持者 (`afx keys` や marker を書く前の `afx init`) は `unknown (daemon 稼働中とみられる)` と出る。これは §12 に残余として書く。

稼働状態の正本は `afx ctl status` である。offline status が稼働 daemon と競合して詳細を読むことはしない。

### 6.2 データ源

| 出力 | データ源 | 読み方 |
|---|---|---|
| setup | `data/state/setup.json` | `read_setup_record(root)`。`ok / missing / corrupt` をそのまま出す (`store/setup_record.py:116-125`) |
| settings | `config/settings.yaml` | 他 source と分離して `ok / missing / invalid` を返す。missing / invalid でも後続 collector を止めない |
| pending | setup record、optional settings、optional DB、`.env` の鍵名、plugin inventory | source ごとの collector から `PendingFacts` を合成し、`pending_steps(..., for_status=True)` へ渡す。外部接続を probe しない (`setup/pending_facts.py:32-65`) |
| ops | `data/ops/<feature>.stage.json` と enroll marker | `read_stage(..., missing_ok=True)`、`read_marker()` (`ops_setup/stages.py:70-89,128-150`) |
| units | user unit file、systemd、linger | unit の有無と `is-active`。`is-active` の stdout が `active` / `activating` / `deactivating` / `inactive` / `failed` ならその語、systemd が無い・bus に繋がらない・stdout が空か未知の語なら `unknown` (rc 1 にしない)。`inactive` は systemd がそう答えたときだけ |
| plugin inventory (pending 用) | `plugins/` directory と §6.4 の観測用 DB 接続 | daemon と同じ `plugin_loader.approved_plugins(conn, plugins_dir, settings=settings).inventory.metas`。`plugins/` 不在、settings か DB が無いときは `()` (観測できた「無い」)。inventory の構築そのものが例外のときは `()` にせず `pending steps: unavailable` (rc 1)。directory も DB も作らない |
| recent mission | §6.4 の観測用 DB 接続で `missions.recent(conn, 1)` | DB が無ければ `none`、破損 / schema 不足は `unavailable` (`store/missions.py:258`) |
| pending approvals | §6.4 の観測用 DB 接続で `approvals.pending(conn)` | 件数だけ出す (`store/approvals.py:263`) |
| plugin deploy ops | `count_open_readonly(db_path, opener=<§6.4 の helper>)` | `none / N / unavailable` を区別する (`store/plugin_deploy_ops.py:272-300`) |
| recent activity | `logs/activity.log` | `tail_lines(..., 1)`。行を `\t` で分けてから field ごとに redactor を通し、本文は 200 字 (行全体を redact すると 1 か所の該当で全文が消え、分割できなくなる) |
| kill switch / mode | `data/state/app_state.json` と reset marker `app_state.json.reset-in-progress` (`store/state.py:89,148-150`) | §6.5 の読取専用 state reader で読む。`instance: unknown` のときは呼ばない |

setup、settings、DB、state、ops、unit、activity、plugin はそれぞれ独立 collector とし、入力と失敗を共有しない。settings や DB が無いときはそれを要する pending fact だけを集めず、その欠落は `settings:` 行と `database:` 行で示す。鍵の存在、unit、ops marker、activity など残りは表示する。DB、lock、設定、unit、log、directory を作らない。初期化関数と migration を呼ばない。

空 root、主要 file 不在、壊れた settings、DB 不在を含むすべての root で、実行前後の directory tree (各 path、type、byte content) が同一であることを契約にする。例外は §6.4 が名指す `data/agentic.db-wal` と `data/agentic.db-shm` の 2 path だけである。主 DB file `data/agentic.db` は例外ではなく、byte 一致を保つ。

`collect_pending_facts` に keyword-only `service_active: bool | None = True` を足す。返す `PendingFacts.service_active` も `bool | None` とし、その値を渡す。daemon と wizard の既存呼び出しは既定 `True` を保つ。offline status は `instance: free` のとき `False`、`instance: unknown` のとき `None` を渡す。現在の固定 `service_active=True` は削除する (`setup/pending_facts.py:32-65`)。

同じ関数の位置引数 `settings` と `conn` は、型を `Settings | None` と `sqlite3.Connection | None` に広げる。offline status は settings が missing / invalid のとき `settings=None`、DB が missing / unavailable のとき `conn=None` で呼ぶ。別の offline 専用 aggregator は作らない。

| 入力 | 集めない fact | `PendingFacts` に入る値 |
|---|---|---|
| `settings is None` | 履歴件数、必要な env 鍵 | `settings=None`、`history_counts={}`、`required_env_keys=()`、`env_present_keys=frozenset()` |
| `conn is None` | 履歴件数、未終端の承認 | `history_counts={}`、`approval_ids=()`。SQL を 1 回も発行しない |

`PendingFacts.settings=None` は `pending_steps()` が既に受け付ける (`setup/pending_steps.py:26,39-48`)。daemon と wizard の既存呼び出しは settings と conn を必ず渡すので、挙動は変わらない。

### 6.3 lock が空いているときの固定出力

```text
agentic-fx offline status
instance: free
setup: <ok|missing|corrupt>
settings: <ok|missing|invalid>
unit: afx.service=<installed|missing> active=<active|activating|deactivating|inactive|failed|unknown> linger=<enabled|disabled|unknown>
ops: bot=<stage|missing|invalid|unavailable> web=<stage|missing|invalid|unavailable> notify=<stage|missing|invalid|unavailable> marker=<none|feature|invalid>
state: mode=<learning|trading|unavailable> kill_switch=<clear|latched|unavailable>
database: <ok|missing|unavailable>
recent mission: <#id state at|none|unavailable>
pending approvals: <N|unavailable>
plugin deploy ops: <N|none|unavailable>
recent activity: <timestamp category event|none|unavailable>
pending steps: <N|unavailable>
  - <既存 render_pending_step の文>
note: この表示は観測時点の情報です。複数データ源の原子的 snapshot ではありません。
next: afx init | afx check | systemctl --user start afx.service | afx ctl status
```

`instance: unknown` の場合も同じ形を出すが、`state: mode=unavailable kill_switch=unavailable` として state reader 自体を呼ばない。`next` は観測結果から 1 つだけ選ぶ。setup missing / corrupt は `afx init`、settings missing / invalid は `afx check`、unit があり `instance: free` で active が `inactive` / `failed` / `unknown` なら `systemctl --user start afx.service`、それ以外 (active / activating / deactivating を含む) は `afx ctl status` とする。鍵 file は locked / free / unknown の判定に使わず、鍵が一つも無くてもこの固定出力を出す。値が無いことと読めないことを混同しない。各 source の失敗で traceback を出さず、他の source の表示を続け、読めない節だけ `unavailable` にする。`unavailable` は「読もうとして読めなかった」(例外、壊れた DB、table 欠落、lock の待ち切れ) にだけ使う。file の形が違う `invalid` / `corrupt`、systemd が無い `unknown`、file が無い `missing` / `none` は観測できた値であり `unavailable` ではない。終了コードは、どこかの節が `unavailable` なら 1、そうでなければ 0 とする (`invalid` / `unknown` / `missing` は rc を変えない)。ops の stage file は、形が違えば `invalid` (rc 0)、読取そのものが例外なら `unavailable` (rc 1)。DB の 3 行 (recent mission / pending approvals / plugin deploy ops) はそれぞれ別の読取であり、1 つの table が欠けても他の 2 行は出す。

### 6.4 観測用の読み取り専用 DB 接続

既存の `connect_readonly` (`store/db.py:549-575`) と `open_readonly` (`store/plugin_deploy_ops.py:272-282`) は `file:…?mode=ro` で開く。DB は `connect()` で `journal_mode=WAL` になっている (`store/db.py:543`)。WAL の DB を `mode=ro` で開いて SELECT すると、`-wal` と `-shm` が無ければ作られ、close 後も残る。既存の `-shm` があれば reader の read-mark 書込みで byte が変わる。このままでは zero-write の契約が成立しない。

そこで `store/db.py` に観測用の helper `connect_observer_readonly(db_path)` を新設し、`afx status` と `afx plugin list` の DB 読取はすべてこれを通す。既存の `connect_readonly` と `open_readonly` の他の呼び出し元は変えない。`count_open_readonly` には接続関数を差し替える keyword `opener` を足し、既定は従来の `open_readonly` とする。

| 開く前の sidecar | 開き方 | 起こり得る書込み |
|---|---|---|
| `<db>-wal` と `<db>-shm` がどちらも無い (lstat で確認) | `file:…?mode=ro&immutable=1` | なし |
| どちらか一方でもある | `file:…?mode=ro` | `-wal` / `-shm` の作成と内容の変化だけ |

主 DB file は、どちらの開き方でも書かれない。読み取り専用の接続は checkpoint できないからである。`immutable=1` は並行する writer を無視する。sidecar が無いのは最後の接続が正常に閉じて WAL が主 DB file へ戻されたときなので、読む内容は主 DB file と同じである。probe の後に daemon が起動して書き始めた場合は、古い内容を読むか読取が失敗する。失敗は該当節の `unavailable` にし、ずれは「観測時点」固定文で開示する。

`mode=ro` で sidecar が作られる、または変わるのは、すでに sidecar がある DB だけである。これは稼働中または直前まで稼働していた daemon の WAL であり、次の daemon 起動でそのまま使われる。AC-9 と AC-29 はこの 2 path だけを例外として名指しする。

### 6.5 読取専用の state reader

state は `data/state/app_state.json` (最上位 `service.py:105`) と、その lock file `app_state.json.lock` を使う。`StateStore.load()` と `StateStore._shared()` は使わない。どちらも lock file を `O_RDWR|O_CREAT` で先に開こうとし、無ければ作るからである (`store/state.py:119,186-191`)。

新しい reader は `_shared()` (`store/state.py:175-199`) と同じ契約を、作成なしで実装する。

1. 親 directory `data/state/` が無ければ、writer も居ないので lock なしで読む (結果は missing)。
2. `app_state.json.lock` を `O_RDONLY` で開く。`O_CREAT` は付けない。
3. lock file が無ければ lock なしで読む。writer は temp file からの atomic replace で書くので、旧 file か新 file のどちらかが読める。
4. lock file があれば `flock(LOCK_SH|LOCK_NB)` を試す。取れなければ 50 ms 間隔で再試行し、合計 1 秒で諦めて `state: mode=unavailable kill_switch=unavailable` とする。
5. 取れたら state file と reset marker を読み、直ちに解放する。

この lock は state file の writer との読取一貫性だけを守る。instance lock とは独立であり、instance lock の判定と state の読取の間のずれは「観測時点」固定文で開示する。

## 7. 文書

### 7.1 原則の正本

原則の説明は `docs/operations/ops-api-2026-10-09.md` の冒頭に「CLI の置き場の原則」という 1 節を置く。7 群 × 2 レーンの表、ラベル、正所属、発注系 API が無いことを書く。

README はその節へリンクし、最初の使い分けだけを書く。`CLAUDE.md` は gitignore 対象で worktree に無いため正本にせず、release の手動同期対象とする。原則本文を README / CLAUDE.md へ複製しない。

### 7.2 更新対象

| 文書 | 更新 |
|---|---|
| `README.md` | `afx status` と `afx ctl status` の使い分け、原則節への link |
| `docs/operations/ops-api-2026-10-09.md` | 原則の正本、新 endpoint 4 本、scope、policy の managed / unmanaged と旧形式の移行、`policy show` の `projection` (`ok` / `stale` と理由) と `policy reconcile`、`policy remove` が managed だけを消せること、`backlog list [--all] [--limit N]`、API に無い操作、`afx ops <feature> <verb>` の新形 |
| `docs/operations/first-run-acceptance.md` | `check backend`、`backlog add`、offline status (`instance:` の 3 語)、policy show/remove/reconcile、`afx ops <feature> <verb>` の手順と期待出力 |
| `docs/operations/afx-service-2026-09-26.md`、`docs/operations/afx-service-direct-exec.md` | `afx ops` の旧形 (各 1 か所) を新形へ置換 |
| `docs/operations/improve-floor-revert.md` | status の選択と plugin 誘導 |
| `docs/superpowers/specs/2026-10-04-ops-api-design.md` | endpoint 25 → 29、route / scope 表 |
| `docs/superpowers/specs/2026-10-16-plugin-deploy-ops-design.md` | `plugin list` と status 誘導 |
| `docs/superpowers/specs/2026-08-16-phase2-10-improve-loop-design.md` | 現行運用名を `check backend` へ注記。履歴本文は改変せず追補 |
| `docs/superpowers/specs/*first-run-wizard*` | wizard が起動する argv と案内文、段 12 の方針文が managed になること、`afx ops <feature> enable` の新形 |
| `CLAUDE.md` (非追跡) | release 手動手順で「CLI の置き場の原則」を ops-api の正本節への 1 行 link に同期。`git check-ignore -q CLAUDE.md` と `test -f CLAUDE.md` を確認し、`rg -n 'CLI の置き場の原則|ops-api-2026-10-09' CLAUDE.md` で link を確認する。commit / AC の対象外 |

利用者に見える製品内の文言も同じ束で新形へ置き換える。`afx ops` の旧形は `setup/wizard.py:6,87,92,1639,2094,2237`、`setup/pending_text.py:117,120`、`ops_setup/stages.py:324,411`、`ops_setup/interactive.py:1` にある。段 12 の案内 `afx ctl improve add` (`setup/wizard.py:2032`) も `afx ctl backlog add` へ置き換える。

survey §2 の集計は 6 パターンで 248 行 / 46 ファイルであるが、一括置換はしない。現行の operations 文書、README、現行 spec のみを更新する。closed ticket、過去 plan、既存 spec の変更履歴は歴史資料として区別し、履歴本文は書き換えず必要なときだけ旧名の追補を足す。

`tests/acceptance/test_first_run_acceptance_doc.py:131` の文言 pin を更新する。実行可能な doc test は (a) ops-api 正本節の 7 群が固定順で出る、(b) README がその anchor へ link する、(c) 正式名 9 つ `afx status`、`afx ctl status`、`afx check backend`、`afx ctl backlog add`、`afx ctl backlog list`、`afx ctl policy show`、`afx ctl policy remove`、`afx ctl policy reconcile`、`afx plugin list` が正所属で出る、(d) 現行 README / `docs/operations/` の手順に `afx improve verify-backend`、正式名としての `afx ctl improve add`、`afx ops enable|disable|status <feature>` の旧形が 0 件、(e) local / ctl 各操作のラベルが記載される、(f) `afx ops <feature> enable|disable|status` の新形が `first-run-acceptance.md` と ops-api 文書に出る、(g) ops-api 文書に `afx ctl backlog list [--all] [--limit N]` の usage 文字列と、`afx ctl policy remove` の説明に「削除できるのは ID marker 付きの managed 行だけで、marker 外の手書き行と legacy 行は削除できない」の文が出る、を検査する。(c) と (g) は、文書からどれか 1 つのコマンドや文を消す変異で落ちることを確かめる。(d) の旧形検出は正規表現 `afx ops (enable|disable|status) (bot|web|notify|<feature>)` で行う。131 行目の `"afx ops status"` の pin は新形 `"afx ops web status"` 等の実際の手順文へ更新する。CLAUDE.md は非追跡なのでこの test に含めない。

## 8. 裁定の記録

### 8.1 確定した裁定

| 日付 | 裁定 | 本書への反映 |
|---|---|---|
| 2026-10-15 | daemon の生きた状態が要る操作は `afx ctl`、要らない操作は最上位 `afx` に置く | §1 のレーン定義、§2 の全表 |
| 2026-10-17 | 7 群 × 2 レーン、危険度は別ラベル、ctl は分類対象外、indicator / strategy は分けない | §0、§2、§3 の help |
| 2026-10-17 | 横断表示は正所属 1 つから誘導する | `status` を群 2、`plugin` を群 6 だけに掲載 |
| 2026-10-17 | help は分類メタデータから生成する | §3 の `cli_surface.py` と一致 test |
| 2026-10-17 | (1)〜(6) の整理を行う | §2、§5、§6、§7 |
| 2026-10-23 | (7) `afx ops <feature> <verb>` の階層にする。動詞は `enable` / `disable` / `reconfigure` / `status`、`reconf` は `reconfigure` の別名。旧形 `afx ops enable\|disable\|status <feature>` は 1 版だけ案内つきの別名で次版削除。分類は「導入・接続管理」、ラベルは `[変更]` (status は `[読取]`)。`reconfigure` の本体は web-lan spec が持つ | §0、§0.1、§2.1、§2.2、§3.2.1、§4.1、§7.2、AC-36〜40 |

### 8.2 確認事項

製品方針として残るのは次の 1 件だけである。

1. **対話 shell の寿命。** 本書はこの release (`0.2.0`) で shell を残し、help 末尾に deprecated 固定文を 1 行出す。既定案は次 major (`1.0.0`) で素の TTY `afx` shell を削除し、`afx ctl` へ一本化することである。**裁定 (2026-10-24、ユーザー承認): 既定案のとおり。`0.2.0` で shell を残して deprecated 固定文を出し、`1.0.0` で素の TTY `afx` shell を削除して `afx ctl` へ一本化する。** I1 の固定文 `1.0.0 で削除予定` はこの裁定に基づく。

旧 (2) は技術決定で閉じた。§5.1 / §5.5 のとおり `logs.read` を流用せず新 scope `guidance.read` を operator / approver にだけ与え、ops-web / ops-bot / ops-notify には与えない。

## 9. 受入条件

上限 `L` を持つ条件は `L-1 / L / L+1` を入れて観測する。

| ID | 条件 | 観測点 |
|---|---|---|
| AC-1 | `afx --help` の stdout 全体が §3.2 と byte 一致し、7 群の順、左右レーン、local ラベルが固定される。ラベル表記は `[読取]` / `[変更]` / `[下位操作ごと]` だけで、「用途」表にはラベルが無い。全角 2 桁で数えてどの行も 100 桁以内である。choices に `improve` / `news` は出ない | stdout 全文 golden、表示幅の検査 |
| AC-2 | `afx ctl help` が §3.3 と byte 一致し、末尾が「API に発注・SL 変更・クローズ・サービス停止は無い。」で終わる | stdout、rc=0 |
| AC-3 | argparse の全正式 local command (`ops` は機能 → 動詞の 2 段)、ctl `parse()` の全正式語順、`exposed=True` の surface record が一対一である。`exposed` なら `implemented`、planned なら非 exposed、alias / compatibility record は通常 parser と独立である。`import agentic_fx.cli_surface` 後の `sys.modules` にある `agentic_fx` 配下は `agentic_fx` と `agentic_fx.cli_surface` だけである | 独立 expected table と集合比較、子 process の `sys.modules` |
| AC-4 | B1 の完了時に endpoint 26 本、B2b の完了時に 29 本で、`_ENDPOINT_COUNT`、routing test の個数 pin 2 か所、独立 `_EXPECTED_TABLE` が一致し、listener が起動する。I1 で API record の endpoint code / scope / mutating の文字列が `ENDPOINTS` と一致する。I1 まで新 record は help 非表示 | `ops/api_server.py:274-281` と routing test 2 か所、surface ↔ `ENDPOINTS` 照合、help snapshot |
| AC-5 | `[確認]` と `_CONFIRM_OPS`、`[approver]` と `_APPROVER_OPS` が一致する。`jobs cancel` は確認あり / approver なし | metadata と client 定数の集合比較 |
| AC-6 | argv shim は先頭語が `improve` の argv (`afx improve`、`afx improve --help`、`afx improve verify-backend …`) すべてで固定文を stderr に 1 回出し、rc=2、backend 接続・DB 作成・settings 書換えが 0 回である。`--daemon` の前置きが 0 個 / 1 個 / 2 個 (`afx --daemon improve --help`、`afx --daemon --daemon improve verify-backend …`) のどれでも同じ結果で、argparse の `invalid choice` にならない。`--dae improve` は現行どおり usage error である。`deprecated_since=0.2.0`、`remove_in=0.3.0` で、整数 tuple 比較による package version を 0.3.0 へ変異させると期限 pin が失敗する。`importlib.metadata.version("agentic-fx")` は各 compatibility record の `deprecated_since` 以上であり、`pyproject.toml` は `0.2.0` である | shim spy、tmp dir、version monkeypatch、installed metadata |
| AC-7 | `afx ctl improve add x` と `afx ctl backlog add x` が同じ `Op("backlog_add", ["x"])`、同じ endpoint、同じ認可、同じ idempotency payload、同じ送信 body、同じ成功 / 失敗 rc になる。差は `Op.via_alias` (`"improve add"` と `None`) だけで、旧名だけ固定案内を stderr に出す。`via_alias` は送信 body と idempotency payload に現れない | parse と fake API の送信記録 |
| AC-8 | `check` は外部接続 0 回で読み取り専用を保つ。`check backend` は §4.2 の非隔離 / 課金警告を stdout へ byte 一致で出し、`OPERATOR_SHELL_NO_ISOLATION` sentinel で指定 backend へ 1 往復し、DB を作らない。backend / provider の不正値は送信前 rc=2。親の `--json` / `--unit-env` と `backend` の併用は §4.2 の固定文を stderr に出して送信前 rc=2。FS before / after の比較範囲は `root` 配下に限る (`verify_backend` はシステムの tmp に作業場を作って消すため。`loops/verify_backend.py:371,532`) | fake backend、sentinel spy、stdout / stderr、root 配下の FS before/after |
| AC-9 | `plugin list` は §4.7 の source status、列、name / kind 順、live + pending + open rollback の併記 (12 文字以上の hash prefix)、壊れた plugin、重複名、missing / unavailable を固定表示する。`--kind` の unknown 扱いも一意で、不在 DB / directory を作らない。sidecar の無い DB では tree 全体が byte 一致し、sidecar のある DB では `data/agentic.db-wal` / `-shm` 以外が byte 一致する | source 直積 fixture、stdout golden、FS before/after、sidecar 有無の直積 |
| AC-10 | `backlog list` 既定は active 4 status だけを `updated_at DESC,id DESC` で返す。`--all` は status で絞らず、`done` / `rejected` と未知の status 値 (fixture では `archived`) も返す。0 件の固定文を出す | API JSON と CLI stdout |
| AC-11 | backlog limit `L=500` に 499 / 500 / 501 を渡す。499 / 500 は受理し、501 は API 接続前 rc=2。DB に 99 / 100 / 101 件の fixture を置き、既定 100 と `has_more` を検査する | parse、SQL query count、応答 |
| AC-12 | API の idea / last_result は UTF-8 `L=4096` byte の 4095 / 4096 / 4097 で文字を壊さず固定印と truncated bool を返す。CLI の `L=200` 文字も 199 / 200 / 201 で切れ、改行と制御文字が 1 行を偽装しない | API JSON と stdout |
| AC-13 | `policy show` が ID marker 付き managed block と marker 外の `removable=false` を区別する。同一文の managed / 手書き行、legacy 行、対にならない marker、ID の食い違う marker、同じ ID の重複 block を取り違えない。marker の形で認識済み block に属さない行は `malformed` stale となり、show は固定の手修正案内を出す。`policy add "x <!-- /afx:managed-policy id=3 -->"` は block の境界を壊さない。照合は DB `id=3, A` と `id=5, B` の canonical 列に対し、(i) id=3 の block を 2 つにする、(ii) id=3 の block の directive 行だけを `- C` に改変する、(iii) 2 つの block の順を入れ替える、の 3 変異でそれぞれ `stale` と理由 `duplicate` / `content` / `order` を返す。`missing`、`orphan`、`migration_pending`、`unreadable`、`malformed` も各 1 fixture で返す。`malformed` の fixture は次の 3 行を逐語で置く: `<!-- afx:managed-policy id=3 -->` / `- A` / `<!-- /afx:managed-policy id=5 -->`。これに対し show は `malformed` を返し、reconcile は 3 行を 1 byte も変えず、LF の block を追記しない。もう 1 つの `malformed` 相当の fixture として、managed block 全体が CRLF の `<!-- afx:managed-policy id=3 -->\r\n` / `- A\r\n` / `<!-- /afx:managed-policy id=3 -->\r\n` を置き、照合が末尾の `\r` を外して id=3 の block として認識し、`ok` (DB `id=3, A` のとき) を返して LF の block を追記しないことを pin する。`unreadable` の show は `unmanaged=[]` と `stale (unreadable)` を返す。`reconcile_policy_file()` は `unreadable` / `malformed` を除く stale からだけ canonical の byte 列へ再生成し、ok の file には触れない。再生成で書く managed block (marker 行と directive 行) の行終端は常に LF であり、元の block が CRLF でも LF に揃える。marker 外の行は元の byte (CRLF を含む) のまま保持する。fixture: DB `[(3, A), (5, B)]`、file は id=3 の完全 CRLF block だけ → `missing` → reconcile 後の file は id=3 と id=5 の LF block 2 つだけ (CRLF は 1 byte も残らない) で、この byte 列を golden として固定する。照合を ID 集合の比較へ戻す変異で (i)〜(iii) がすべて落ちる | 合成 DB と directives file、`inspect_policy_file()` の戻り値、file の byte 比較 |
| AC-14 | policy 合計上限 `L=200` について 199 / 200 / 201 件を入れ、順序、件数、`has_more` を検査する。directive 表示 `L=200` 字も 199 / 200 / 201 で検査する | API JSON と CLI stdout |
| AC-15 | `policy remove <id>` は該当 DB row だけを消し、DB の全 row から ID marker 付き block を file 末尾に再生成し、marker 外の手書き / legacy / 不正 marker を byte 保存し、成功時だけ固定 activity を 1 件残す。`ops_requests` の phase は `accepted` と `succeeded` の 2 行だけで、CHECK 制約の 4 値以外を書かない | DB / file / audit / activity の before-after |
| AC-16 | (a) remove の同一 idempotency key 再送は保存済みの応答を返す。(b) DELETE commit 後の file replace 例外は、`ops_requests` に phase=`outcome_unknown` の終端行を書き、`outcome_unknown` を応答する。(c) DELETE commit 後・replace 前の hard crash を fault injection し、再起動で `recover_after_journal()` がその要求を `outcome_unknown` にし、file が DB 正本へ収束する。(d) managed が 1 件だけの状態でその 1 件を remove し、同じ位置で crash しても、再起動後の file に block が残らない (DB 0 行でも reconcile が走る)。(e) (b) の後に別 key で同じ ID を remove すると、`not_found` を返す前に file が収束し、先頭 reconcile が直した場合の `policy_reconciled` activity は残る。daemon は再起動しない。(f) 別 key で削除済みの正の整数 ID を送ると、API は `not_found` を返し、先頭 reconcile が unchanged なら DB / file / activity は変わらない。`0`、負数、非数値 (`abc`、`1.5`、空文字) は client の parse で API 接続前に rc=2 となり、鍵の読込と接続は 0 回である。(g) (b) の後の file に AC-13 の (i)〜(iii) の変異を重ねても、別 key の remove の先頭 reconcile で canonical 列へ収束する。(h) 起動時 reconcile が例外でも `SYSTEM policy_reconcile_failed` と技術ログを残して listener は開き、`recover_after_journal()` の失敗だけが listener を妨げる | replace fault、crash child、再起動 recovery、listener、API と client parse |
| AC-17 | principal × scope / 新 endpoint が §5.5 と一致する。`guidance.read` は operator / approver だけが持ち、ops-web / ops-bot / ops-notify は backlog / policy 自由文を取得できない。remove と reconcile も operator / approver だけが通る | principal × scope × endpoint の直積 test |
| AC-18 | `afx status` は instance lock が排他保持されているとき §6.1 の `instance: locked` の 5 行だけを出し、DB / state / log reader を呼ばない。`locked-by` は setup marker / enroll marker / 両方 (enroll が先) / 読めない enroll marker / どちらも無い、の各場合に §6.1 の語になり、`next` は §6.1 の表どおりになる。どちらの marker も無く鍵 file も無いときは `next` が `journalctl --user -u afx.service …` になる。marker と鍵は作成・変更しない | exclusive lock holder fixture、marker 直積、鍵有無、spy |
| AC-19 | `instance: free` で setup=`ok / missing / corrupt`、settings=`ok / missing / invalid`、主要 file 不在を固定語で出す。どの source が不良でも残りの collector と pending steps を表示する | source 直積 fixture |
| AC-20 | DB が ok / missing / corrupt、各 table が present / absent の場合に `ok / missing / unavailable`、mission、approval 件数、deploy op 件数を固定形で出す。DB を作成・migration せず、DB collector の失敗で settings / unit / ops / activity の表示を抑止しない | FS / sqlite fixture の直積 |
| AC-21 | instance lock の shared lock は probe の 1 回だけ取り、state を読む前に解放されている。state は `data/state/app_state.json.lock` を `O_RDONLY` で開いて shared で読み、clear / latched / markerあり / missing / corrupt を出す。lock file 不在は lock なしで読み、lock file を作らない。`app_state.json.lock` が 1 秒排他保持されたままなら `unavailable` になる。`StateStore.load()` / `_shared()` は呼ばず、`instance: unknown` では state を読まない | flock fixture、state-reader spy、open flag の記録 |
| AC-22 | unit / systemd / linger、ops stage 3 feature、enroll marker、activity log が各々 present / missing / invalid の場合に該当節だけが変わる | source ごとの fixture と stdout snapshot |
| AC-23 | `collect_pending_facts(service_active=False)` が pending start を正しく残し、既存呼び出しの省略時は True を保つ。`None` も型と表示が壊れない。`settings=None` と `conn=None` を受け付け、§6.2 の表の値を返す。空 root (settings も DB も無い) では `conn` の spy に届く SQL が 0 回で、例外を出さない | pending steps unit test、connection spy |
| AC-24 | offline status の各 source が例外でも traceback を出さず、読めない節だけ `unavailable`、rc=1 になる。全 source が読めると rc=0 | source ごとの fault injection |
| AC-25 | shell help は `shell=True` から生成され、`backlog add`、`classified=False` の `stop`、deprecated 末尾 1 行を出す。旧 `improve add` は同一動作、`stop` は現行の graceful shutdown 要求になる | shell help snapshot、dispatch spy、既存 16 node の更新 |
| AC-26 | ops-api の 7 群順、README anchor link、§7.2 (c) の正式名 9 つ、local / ctl ラベルが存在し、現行手順の旧名が 0 件である。ops-api 文書に `backlog list [--all] [--limit N]` と `policy remove` の managed だけという制約が出る。どれか 1 つを文書から消す変異で落ちる。CLAUDE.md は release 手動確認 | `test_first_run_acceptance_doc.py` と追跡文書 grep pin |
| AC-27 | `news` は `planned=True, implemented=False, exposed=False` で、本 release の choices / help / parser / API endpoint のいずれにも出ない | full help golden と planned 例外 pin |
| AC-28 | daemon 稼働中の `ops enable` は rc=1 拒否へ変異されず、marker → stop → lock → enroll / install → unlock → restart の順で完了する。元が停止中なら再起動しない | 既存 durable stage test と mutation test |
| AC-29 | 空 root、主要 file 不在、settings 破損、DB 不在、鍵不在、DB sidecar 有り / 無しの locked / free / unknown すべてで、実行前後の tree が同一である。例外は sidecar 有りのときの `data/agentic.db-wal` と `data/agentic.db-shm` だけで、`data/agentic.db` は byte 一致する。sidecar 無しの DB では sidecar が作られない | recursive tree manifest と byte compare |
| AC-30 | instance lock 不在 / unreadable は `instance: unknown`、state reader 0 回となり、locked / free / unknown すべてで「観測時点」固定文を出す | lock fault injection、stdout snapshot、spy |
| AC-31 | `afx status` が state / DB を読んでいる最中 (reader に待ちを差し込む) に、別 thread の `acquire_instance_lock(root / "data")` が成功する。`afx status` の全実行中に instance lock を保持する時間は probe の 1 回だけである | lock holder thread と並行させた test、flock 呼出しの記録 |
| AC-32 | marker の無い旧形式 `directives.md` と `ops_policies` の fixture から起動すると、file が期待 byte 列と一致し、`data_migrations` に `policy_directives_managed_v1` が 1 行入る。fixture は DB と file で重複数を変える。DB は `A` が 1 行と `B` が 2 行、file は `- A` が 2 行、`- B` が 1 行、手書き行 `- C`、DB 行の無い shell 由来の `- D`、CRLF の手書き行 `- E\r\n` を各 1 行含む。移行後の file は `- C`、`- D`、CRLF 行を元の byte で残し、`- A` / `- B` の marker 外の行は 1 行も残さず、block を A 1 つ、B 2 つ持つ。Mission の注入文には A が 1 回、B が 2 回、C と D が 1 回ずつ現れる。別 fixture で ID の食い違う終了 marker を含む 3 行 `<!-- afx:managed-policy id=3 -->` / `- A` / `<!-- /afx:managed-policy id=5 -->` を置くと `malformed` stale となり、移行・replace をせず byte と migration record が不変である。さらに別 fixture で managed block 全体が CRLF (`<!-- afx:managed-policy id=3 -->\r\n` / `- A\r\n` / `<!-- /afx:managed-policy id=3 -->\r\n`) の file から起動すると、block は id=3 として認識され、LF の block を追記せず、Mission の注入文に A は 1 回だけ現れる。移行後に A の行を `policy remove` すると、A は注入文から消え、B の 2 回は変わらない。file replace 後・記録前の crash からの再実行と 2 回目の起動は file を変えない。file 不在かつ DB 0 行では migration record だけを残し、file / directory を作らない | 旧 fixture → byte 一致、CRLF / malformed fixture、`Policy.tail()` の出力、crash child |
| AC-33 | `Policy.tail()` と `size_warning()` は marker 行を除いた文で計算する。marker を含む file で末尾 4000 文字に marker の断片が入らない。`L=4000` について marker 除去後 3999 / 4000 / 4001 文字の file を検査する | `Policy` の unit test |
| AC-34 | 初回設定の段 12 で入れた方針文 (例文 `e` を含む) は `ops_policies` の行と managed block になり、`policy show` で `managed` に出て `policy remove` で消せる。DB insert 前の失敗と commit 後の file 反映失敗はそれぞれ §5.3.4 の固定文を出す。shell の `ops_service is None` 経路も同じ managed 形式で書く | wizard の段 12 test、fault injection、shell fallback test |
| AC-35 | `policy add` で INSERT commit 後の file replace 例外は phase=`outcome_unknown` の終端行と `outcome_unknown` 応答になり、activity `policy_added` を残さない。同じ key の再送は `outcome_unknown`、次の add / remove / reconcile または再起動で file が DB へ収束する。その間の `policy show` は file を変えずに `stale` を返す。add / remove の先頭 reconcile で `MigrationRecordUnknown`、`unreadable`、`malformed` が起きたときは DB を変えず `failed` (`internal`) で終え、`outcome_unknown` へ写さない | replace fault、audit / activity の before-after |
| AC-36 | `afx ops <feature> <enable\|disable\|status>` と旧形 `afx ops <verb> <feature>` が、3 機能 × 3 動詞 × `--daemon` 前置き 0 / 1 / 2 個のすべてで同じ dispatch (`interactive_enable(root, feature)` / `run_ops_command(root, verb, feature)`) を同じ引数で 1 回呼び、同じ rc を返す | entry の dispatch spy |
| AC-37 | 旧形は §2.2 の固定文を stderr に 1 回だけ出し、新形は何も出さない。旧形で機能名が無い / 不正なときは案内を出して rc=2、dispatch 0 回である。どちらも `--daemon` 前置き 0 / 1 / 2 個で同じ結果になり、書換えは head からの位置で行われ、前置きの `--daemon` は残る (parse 後の `daemon` 属性が書換え前の argv と同じ)。旧形 record は `deprecated_since=0.2.0`、`remove_in=0.3.0` で、package version を 0.3.0 へ変異させると期限 pin が失敗する | stderr、dispatch spy、version monkeypatch |
| AC-38 | web で `afx ops web reconf` と `afx ops web reconfigure` が同じ `run_ops_reconfigure(root, "web")` を 1 回呼び、同じ rc を返し、stderr に案内を出さない。`--daemon` 前置き 0 / 1 / 2 個でも同じである。bot と notify では `reconf` と `reconfigure` の両方が argparse の usage error (rc=2) で、handler 0 回である | entry の dispatch spy (handler は stub) |
| AC-39 | `afx ops --help`、`afx ops web --help`、`afx ops bot --help`、`afx ops notify --help` の stdout 全体が §3.2.1 の 4 つの golden と byte 一致する。golden は `FORCE_COLOR`、`NO_COLOR`、`PYTHON_COLORS`、`TERM` 等を除いた環境で実行する (非 TTY capture で取り、`color=False` は 3.14 以上でだけ渡す。3.13 の argparse では `TypeError`)。`reconfigure` は web の choices と一覧、`afx ops --help` の `afx ops web reconfigure` 行にだけ出る。bot / notify の record は `implemented=False, exposed=False` である。どの usage の choices にも `enable` / `disable` / `status` が機能の位置に出ず、`reconf` も出ない。全行が 100 桁以内である | stdout 全文 golden、record の値 |
| AC-40 | 追跡文書 (§7.2 の doc test (d)) と製品の文言 (`setup/`、`ops_setup/` の利用者向け文字列) に `afx ops (enable\|disable\|status) <feature>` の旧形が 0 件である。`first-run-acceptance.md` と ops-api 文書には新形が出る | `test_first_run_acceptance_doc.py`、`src/` の文字列 grep pin |
| AC-41 | `policy show` は純粋な読取である。ok / stale (`missing`、`orphan`、`duplicate`、`content`、`order`、`migration_pending`) の各 fixture で、実行前後の directives file、`ops_policies`、`data_migrations`、`ops_requests`、`ops_idempotency`、activity log が byte 一致する。応答の `projection` は §5.3.1 の固定語を表の順で返し、CLI は §5.3.5 の固定行を出して rc=0 である。show の中で `reconcile_policy_file()` を呼ぶ変異で落ちる | file / DB / activity の before-after、reconcile spy |
| AC-42 | `policy reconcile` は `unreadable` / `malformed` 以外の stale の file を canonical の byte 列へ直して `changed=true` と固定 activity 1 件を返し、ok の file では file に触れず `changed=false` で activity を残さない。`unreadable` fixture (mode `000`) は endpoint が `failed` (`internal`) となり、file の inode と mode が不変である。`ops_requests` は成功時に `accepted` と `succeeded` の 2 行で、同じ key の再送は保存済みの応答を返す。replace 前の失敗は `failed` (`internal`)、移行の replace 後・記録前、または replace 後の応答用 inspect 失敗は `outcome_unknown` になる。引数付きの `afx ctl policy reconcile x` は接続前に rc=2 である | DB / file / audit / activity の before-after、inode / mode、fault injection、client parse |
| AC-43 | §3.1 の help の正本の検査が通る。metadata の `usage` は常に完全形で shell の裸 token は `shell_usage` に置く。`entry.py`、`backtest/cli.py`、`ops/client.py`、`commands.py` の `ArgumentParser()` / `add_parser()` の `help` / `description` / `epilog` に str literal が無く、metadata の `summary` / `condition` / 群名、および空白を含む `usage` と完全一致する str 定数がこの 4 file に無い。metadata の summary を 1 つ変えると `afx --help` などの golden がその分だけ変わる | AST 検査、metadata 変異と golden の差分 |

## 10. 変更ファイルと task 表

### 10.1 変更ファイル予定

| 領域 | ファイル | 変更 |
|---|---|---|
| metadata | `src/agentic_fx/cli_surface.py` (新規、ops package の外) | 7 群、2 レーン、command record、help renderer。import は stdlib だけ |
| 最上位 CLI | `src/agentic_fx/entry.py`, `src/agentic_fx/backtest/cli.py`, plugin 表示 module | help epilog、`check backend`、`improve` / `ops` 旧形 / `reconf` の argv shim、`ops <feature> <verb>` の parser、`plugin list`、`status` 配線 |
| offline status | `src/agentic_fx/status.py` (新規), `setup/pending_facts.py`, `store/db.py`, `store/plugin_deploy_ops.py` | instance lock の probe、読取専用 state reader、read-only collector、固定 renderer、`service_active` 引数と `settings` / `conn` の `None` 許容、`connect_observer_readonly`、`count_open_readonly(opener=)` |
| ctl | `src/agentic_fx/ops/client.py` | 生成 help、正式名 / alias parse、list / show / remove / reconcile formatter |
| API | `ops/contracts.py`, `ops/service.py`, `ops/api_server.py`, 最上位 `service.py` | endpoint 4 本、service method、route 26 → 29 本、`_terminal` の state 引数、失敗を記録して listener を妨げない `_recover_ops` の無条件 reconcile |
| store / policy | `store/backlog.py`, `src/agentic_fx/policy_projection.py` (新規), `src/agentic_fx/policy.py` | bounded list、policy の投影・列の照合 (`inspect_policy_file`)・安全に停止する移行 / reconcile、CRLF marker を除いた注入 |
| shell | `src/agentic_fx/commands.py` | `_HELP` 生成、backlog 正式名、policy fallback の managed 化。新 surface は足さない |
| wizard / setup | `src/agentic_fx/setup/wizard.py`, `setup/pending_text.py`, `ops_setup/stages.py`, `ops_setup/interactive.py` | 新 argv と案内文、段 12 の managed 化、`afx ops` 新形の文言 |
| package | `pyproject.toml` | `version` を `0.2.0` へ |
| test | `tests/test_entry_ctl.py`, `tests/ops/*`, `tests/backtest/*`, `tests/setup/*`, `tests/commands/*`, acceptance doc test | §9 の pin |
| docs | §7.2 の現行文書 | 原則、正式名、API、status |

変更予定を列挙するための表であり、本設計書更新ではこれらを変更しない。

### 10.2 task 表

| task | 依存 | 範囲 | 既存 pin (keep / update) | 新設 pin | 完了 AC |
|---|---|---|---|---|---|
| S1 metadata schema (非公開) | なし | `cli_surface.py` の field / renderer interface、`guidance.read` | keep: `tests/ops/test_ctl_pins.py::test_client_does_not_import_setup_persistence_layer`。update: `tests/ops/test_scope_enforcement.py::{test_scope_table_matches_the_spec,test_every_endpoint_admits_exactly_the_principals_holding_its_scope}`、`tests/ops/test_ac56_57_ops_principals.py::{test_ac56_authorization_table_matches_the_spec_for_every_principal,test_ac56_every_route_is_allowed_only_by_the_spec_scope}` | new: exposed ⇒ implemented、planned ⇒ hidden、compatibility version 期限、classified=false shell record、`cli_surface` の import 閉包 | AC-5、27 |
| B1 backlog API / parse | S1 | 正式 add / alias (`Op.via_alias`)、bounded list、`guidance.read`、個数 pin 25 → 26 | update: `tests/ops/test_ac01_02_43_api_routing.py::{test_ac1_all_24_endpoints_over_real_uds,test_ac1_routes_and_endpoints_match_the_independent_table_exactly}` (26 本)、`tests/ops/test_ctl_operations.py::test_backlog_add_and_transition`、`tests/ops/test_ctl_pins.py::test_backlog_idea_echo_is_sanitized`、`::test_backlog_idea_echo_counts_the_removed_characters` | new: list order / limit / 4,096-byte truncation / 全 status / alias equivalence / principal 直積。record はまだ `exposed=False` | AC-4 (26 本)、7、10〜12、17 (backlog 分) |
| B2a policy 投影・移行 | S1 | `policy_projection.py` (`_render_directive` の移設、`insert_policy`、`regenerate_policy_file`、`inspect_policy_file`、`reconcile_policy_file`)、ID marker と §5.3.1 の列照合、CRLF、`unreadable` / `malformed` の安全な停止、旧形式の移行、`Policy.tail()` / `size_warning()` の marker 除去、`OpsService._regenerate_policy` の置換、失敗を記録して listener を開く `_recover_ops` の無条件 reconcile。endpoint は足さない | 判定 (投影の byte 形式に依存すれば update、部分一致だけなら keep。着手前に grep で確定): `tests/test_policy.py` の全 9 node、`tests/ops/test_ac48_50_crash_recovery.py::test_ac35_policy_crash_regenerates_one_line_from_records`、`tests/ops/test_ac35_52_idempotency.py::test_ac35_policy_file_is_regenerated_and_hand_written_lines_survive`、`tests/ops/test_ctl_operations.py::test_policy_add_writes_the_directive`、`tests/ops/test_service_api_wiring.py::{test_ac41_api_policy_through_build_app_and_listener_then_clean_stop,test_startup_rebuilds_the_policy_file_from_its_records}` | new: 同文 managed / handmade、不正 marker、偽造 marker、照合の 3 変異 (重複 block、本文改変、順序入替) と残りの stale 理由、ID 集合比較へ戻す変異、旧形式 fixture (重複数の違う DB / file) の byte 一致、CRLF / malformed / unreadable、移行の記録前 crash、DB 0 行での起動時 reconcile | AC-13、32、33 |
| B2b policy API・audit | B1, B2a | `policy_show` (純粋読取と `projection`)、`policy_remove`、autopilot 中も使える `policy_reconcile`、add / remove の action 先頭の reconcile、`_terminal` の state 引数、endpoint のみが `MigrationRecordUnknown` / replace 後 inspect 例外を `PolicyProjectionUnknown` → `outcome_unknown` へ写す、`guidance.read`、client の parse と formatter、個数 pin 26 → 29 | update: routing test 2 node (29 本)、§5.1 の principal × scope pin 4 node (S1 で `guidance.read` を足した表へ `policy_reconcile` を加える) | new: show の before-after 不変、reconcile の changed / unchanged、commit 後・replace 前 crash、最後の 1 件の crash、別 key 再実行の収束、add / remove の先頭 reconcile は `failed`、unreadable inode / mode 不変、remove の id parse 境界、principal 直積 | AC-4 (29 本)、14〜17、35、41、42 |
| B2c policy の wizard・shell 統合 | B2a | 段 12 の `insert_policy` → `reconcile_policy_file` と 2 種の失敗文、shell の `ops_service is None` fallback の managed 化、`append_policy_directive` の削除 | keep: `tests/commands/test_improve_commands.py::test_policy_add_appends_to_directives_file`。判定 (B2a と同じ基準): `tests/setup/test_wizard_finish.py::{test_policy_lines_and_first_ideas_are_written_one_at_a_time,test_existing_policy_size_and_backlog_count_are_shown_before_asking}`、`tests/setup/test_wizard_guards.py::{test_example_shortcut_adds_all_examples_to_policy_and_backlog,test_example_write_failure_stops_the_policy_prompt,test_policy_and_first_ideas_leave_the_same_activity_lines_as_the_shell}` | new: 段 12 の insert 前失敗 / commit 後の file 失敗、shell fallback の managed 形式 | AC-34 |
| A1 local check / plugin / ops | S1 | argv shim (`improve` 先頭語、`ops` 旧形、`reconf`)、`ops <feature> <verb>` の parser と dispatch、`check backend` と親 option の併用拒否、`plugin list` と観測用 DB 接続、現行 `ops enable` の状態機械 | update: `tests/ops_setup/test_entry.py::{test_ops_enable_dispatches_feature_without_starting_daemon,test_ops_status_reports_saved_operation_stage_and_failure,test_ops_failure_prints_fixed_reason_and_exits_1,test_ops_failure_prints_detail_after_the_fixed_reason,test_ops_marker_rejection_prints_owner_and_recovery,test_ops_status_without_systemctl_reports_unknown_unit}` (argv を新形へ)、`tests/backtest/test_cli.py::{test_cli_improve_verify_backend_routes_to_verify_backend,test_cli_improve_verify_backend_routes_opencode_through_to_verify_backend,test_cli_verify_backend_prints_enable_guidance_for_opencode,test_cli_verify_backend_prints_detail_and_fingerprint_on_success,test_cli_improve_verify_backend_returns_1_when_verification_fails,test_cli_verify_backend_reports_ready_mismatch_as_rc1_not_traceback,test_cli_improve_verify_backend_requires_backend_argument}`、`tests/setup/test_wizard_llm.py::{test_opencode_needs_a_context_limit_and_is_never_marked_verified,test_the_improve_exchange_runs_as_a_child_command_after_the_commit}`、`tests/setup/test_wizard_guards.py::test_a_failed_improve_exchange_is_verify_failed_not_a_connection_error`。keep: `tests/ops_setup/test_stages.py::{test_enable_orders_marker_stop_confirm_lock_enroll_unlock_and_marker_removal,test_enable_with_afx_stopped_beforehand_does_not_start_it,test_enable_crash_at_each_point_resumes_and_clears_marker}` | new: shim 期限、非隔離 / 課金 stdout、sentinel spy、plugin view model 直積、sidecar 直積、稼働中 enable を rc=1 へする mutation killer、新旧 `ops` の dispatch 同一性、`reconf` | AC-3、6 (shim 分)、8、9、28、36〜38 |
| A2 offline status | S1 | instance lock の probe と即時解放、`locked-by` と `next`、読取専用 state reader、観測用 DB 接続、source 独立 collector、renderer | keep: `tests/setup/test_pending_facts.py::{test_collection_limits_history_pairs_and_journal_ids,test_supplied_landlock_availability_is_used_without_probing,test_a_failing_landlock_probe_is_unknown_not_unavailable,test_env_key_presence_combines_dotenv_and_process_environment}` | new: default `service_active=True` / explicit False / None と AC-18〜24 / 29〜31 の lock、marker、鍵無し、source fault、tree zero-write 直積、lock 保持者との並行 | AC-18〜24、29〜31 |
| I1 help / shell / 統合・文書公開 | B1, B2a, B2b, B2c, A1, A2、§8.2-1 の裁定、web-lan spec の main merge | `exposed=True`、help golden (`afx`、`afx ops`、`afx ops web` / `bot` / `notify`)、help の正本の AST 検査、surface ↔ `ENDPOINTS` 照合、shell、`pyproject.toml` の version を `0.2.0` へ、製品文言と追跡文書の `afx ops` 新形 | update / keep は下の node 表のとおり | new: `afx --help` / `afx ops --help` / 3 feature の全文 golden、表示幅、shell `stop`、doc 7 群 / link / 9 正式名 / ラベル / `backlog list` と `policy remove` の文 / 旧名ゼロ / `ops` 旧形ゼロ、version ≥ `deprecated_since`、help literal の重複検査 | AC-1〜5、6 (version 分)、25〜27、39、40、43 |

実装順は **S1 → B1 → B2a → B2b → B2c → A1 / A2 → I1** である。B2b は B1 の後に置く。どちらも `_ENDPOINT_COUNT` と独立表 `_EXPECTED_TABLE` を書き換えるので、並行させると衝突する。B2a は endpoint を足さず、既存の add と起動時の再生成を新しい投影規則へ載せ替える。B2b と B2c は B2a の投影関数を使う。A1 / A2 は各々独立に test-first で完結できる。B1 / B2b は自分の個数 pin を更新するので、単独で listener が起動する。新操作を help へ出すのは I1 だけである。A + B は I1 を含む 1 release とする。

`ops <feature> <verb>` の新形 parser と旧形 shim は A1 で入る。旧形は案内つきで動き続けるので、A1 単独でも利用者の手順は壊れない。help golden、製品文言、追跡文書の置換は I1 で行う。I1 は web-lan spec `2026-10-23-web-lan-access-design.md` と、その web の `run_ops_reconfigure` 本体が main に merge された後に着手する。同 spec は現在 web-lan branch にあり、本 worktree に無いのは merge 前だからである。I1 は web の `reconfigure` を `implemented=True`、bot / notify を `implemented=False` として §3.2.1 の golden を固定する。§8.2-1 の裁定は shell の固定文が依存するので I1 の着手前に取る。

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

12 本のうち 10 本は `usage: afx ctl` などの部分一致なので keep とし、全てを書き換える見積りにしない。endpoint 個数 pin は I1 ではなく B1 (26) と B2b (29) で更新する (§3.4)。

#### I1 の `afx ops` 文言 node

製品文言を新形へ置き換えると、次の node が旧形の文字列を見ているので **update** する: `tests/ops_setup/test_stages.py::test_marker_of_another_feature_is_rejected_without_changes`、`tests/setup/test_wizard_abort.py::{test_another_setup_operation_stops_the_wizard_before_it_touches_the_instance_lock,test_an_unfinished_ops_enable_stops_the_wizard,test_both_markers_report_the_ops_enable_first}`、`tests/setup/test_wizard_finish.py::test_a_failed_enable_does_not_stop_the_wizard_and_is_recorded_with_its_reason`、`tests/acceptance/test_first_run_acceptance_doc.py::test_observation_commands_and_journal_contract_are_present`。数は `rg 'ops (enable|disable|status)' tests src` で着手前に数え直す。

#### I1 の shell backlog / help node

`tests/commands/test_improve_commands.py` の次の 15 node はすべて **update** とし、正式入力を `backlog add` へ変える: `test_improve_add_creates_open_backlog_row`、`test_improve_add_without_text_returns_usage`、`test_improve_add_echoes_the_registered_idea_text`、`test_improve_add_warns_on_short_or_placeholder_idea`、`test_improve_add_does_not_block_registration_when_warned`、`test_improve_add_warning_text_does_not_leak_to_backlog_last_result`、`test_improve_add_warns_and_notes_removed_chars_for_zero_width_input`、`test_improve_add_strips_terminal_control_sequences_from_reply`、`test_improve_add_stores_idea_without_display_normalization`、`test_improve_add_does_not_warn_on_unclosed_bracket_prefix`、`test_improve_add_closing_bracket_must_be_at_the_end`、`test_improve_add_warns_exactly_at_length_boundary`、`test_improve_add_placeholder_detection_uses_normalized_display_not_raw_text`、`test_improve_add_does_not_leak_warning_or_display_into_activity_log`、`test_improve_add_warning_includes_reject_command_with_actual_bid`。

ops lane の 1 node `tests/ops/test_ac09_25_26_decision_lane.py::test_shell_policy_backlog_and_improve_go_through_the_lane` も **update** する。旧 `improve add` と正式 `backlog add` の同一動作は別の **new** parameterized node 1 本に集約する。shell help の `tests/test_commands.py::{test_approval_list_argument_boundaries,test_approval_retry_missing_id_is_value_error_with_help,test_help_includes_approval_list_journal_hint}` は **update** し、**new** の全文 snapshot と `stop` graceful-shutdown node を追加する。「22 本」という文字列出現数は test 数として使わない。

## 11. 後続と非目標

- `afx news` の parser、source 選択、429 backoff は `news-source-cli-and-429-backoff` で行う。
- 対話 shell の廃止は §8.2-1 のユーザー裁定後、次 major 向けの別束とする。本 release は deprecated 表示までである。
- bare `afx` の daemon 起動挙動は本束で変えない。
- 人間が書いた policy の手書き行は API 管理下へ移行しない。システムが書いた旧形式の行は §5.3.2 の規則で 1 回だけ移行する。対象は DB 行と文面が一致する行だけで、DB 行の無い旧 shell / 旧初回設定の行は unmanaged のまま残る。
- `afx ops web reconfigure` の動作本体は `2026-10-23-web-lan-access-design.md` で作る。本書は階層、別名、help、文書だけを扱う。bot / notify の `reconfigure` は本体を作る束で、record と §3.2.1 の golden を同時に改訂する。
- backlog の検索、pagination cursor、CSV / JSON 出力 option は作らない。
- ops-web / bot / notify に backlog / policy の UI や通知を足さない。
- `guidance.read` 以外の新しい認可 scope は作らない。
- `verify-backend-isolated-via-service` は隔離適合の確認を service job で行う将来の `afx ctl` 操作として別束にする。本 spec の `afx check backend` は接続性だけで、隔離適合の合否を返さない。
- plugin 即時承認を API 化しない。
- 発注、SL 変更、クローズ、サービス停止の API を作らない。
- help で色、Unicode 罫線、端末幅ごとの動的折返しを行わない。

## 12. 残余リスク

- 対話 shell は API 認証を通らない。`ShellCaller.SHELL` の監査は残るが、利用者向けの「2 レーン」と実行境界が完全には一致しない。
- policy 投影は DB commit と file replace を同一 transaction にできない。既存の accepted → 終端、`outcome_unknown`、起動時、add / remove の先頭、明示操作 `policy reconcile` の reconcile で repairable な stale を収束させるが、replace 失敗や crash の直後から次の reconcile までは旧投影が Mission に注入され得る。`unreadable` / `malformed` は自動上書きせず手修正を要する。`policy show` はこのずれを `stale` として示すだけで、直さない。
- offline status は複数 file / DB の同時 snapshot ではない。daemon 不在でも観測中に人間の別 CLI が変更すれば、節間で時刻がずれる。表示は診断用で、取引判断の正本にしない。
- instance lock が不在 / unreadable の環境では `instance: unknown` になる。unknown 時は state 詳細を読まず、他 source も「観測時点」の診断としてのみ出す。
- instance lock の probe は shared lock を 1 回取って直ちに解放するが、その瞬間に重なった `LOCK_EX|LOCK_NB` の取得 (daemon 起動、`afx keys`、`afx init` 等) は失敗し得る。窓は flock の system call 2 回の間に限られる。daemon は `Restart=on-failure` で再試行され (`setup/unit.py:47`)、他の操作は利用者が再実行する。
- `locked-by` は marker からの推定である。marker を書かない保持者 (`afx keys`、marker を書く前の `afx init`、post_start) も `unknown (daemon 稼働中とみられる)` と出る。`next` の `afx ctl status` が失敗したら、利用者はその保持者の完了を待って `afx status` を再実行する。
- `immutable=1` の観測用接続は並行 writer を無視する。sidecar の無い DB を読んでいる間に daemon が起動して書き始めると、古い内容を読むか読取が失敗する。表示は「観測時点」の診断に限る。
- `news` は planned metadata だけに存在し、実装まで help に出ない。metadata だけが陳腐する可能性は後続束で解消する。

## 13. 変更履歴

| 日付 | 版 | 変更 | 理由 | commit |
|---|---|---|---|---|
| 2026-10-23 | v0.1 C0 | 7 群 × 2 レーンの完成表、metadata と help、例外裁定、新 API、offline status、文書、AC、2 束の task 表を新規作成 | `cli-surface-principle-and-reorg` の設計下書き | |
| 2026-10-23 | v0.2 | 設計レビュー 1 周目の 11 件、AC-1 / 3 / 4 / 6 / 8 / 9 / 12 / 13 / 15〜21 / 24〜30、依存順、test node 表を反映 | 設計レビュー 1 周目 sol、High 5 / Medium 6 全採用、§8.2-2 を技術決定で閉鎖 | |
| 2026-10-23 | v0.3 | policy を既存 audit の accepted → 終端で表し新 phase を廃止、旧形式 `directives.md` の 1 回限りの移行、marker の行全体一致と注入時の除去、段 12 と shell fallback の managed 化、起動時と endpoint 先頭の無条件 reconcile、add / remove の `outcome_unknown`。offline status を instance lock の probe と即時解放、`instance: locked/free/unknown` と `locked-by`、`app_state.json.lock` の読取専用 reader、WAL sidecar の有無で `immutable=1` / `mode=ro` を選ぶ観測用 DB 接続に変更。metadata を ops package 外の `cli_surface.py` へ移設、個数 pin を B1 で 26・B2 で 28、B1 → B2 の直列化、version を 0.2.0 へ上げる task、L1〜L9 の細部。裁定 (7) の `afx ops <feature> <verb>` 階層、`reconf` 別名、旧形の期限つき互換別名、`afx ops` help golden、文書と製品文言の置換。AC-1 / 3 / 4 / 6〜10 / 13 / 15 / 16 / 18 / 19 / 21 / 29 / 30 を更新、AC-31〜40 を新設 | 設計レビュー 2 周目 opus、High 4 / Medium 9 / Low 9 全採用 + ユーザー裁定 (7) | |
| 2026-10-24 | v0.4 | `improve` の `remove_in` を `0.3.0` に統一。投影の照合を ID 集合から `[(id, directive)]` の順序付き列 (出現数込み) と canonical 列の完全一致へ変更し、stale の固定理由 7 語を新設。旧形式の移行を集合一致の全行除去 + DB 行ごとの block 再構成に変更。`policy show` を純粋読取にして `projection` を返し、明示操作 `policy reconcile` (`POST /v1/policy/reconcile`、`operate`、`[変更]`) を新設、endpoint 29 本。bot / notify の `reconfigure` を `implemented=False, exposed=False` に固定し、`afx ops` の 4 golden を確定。help 文言を全て metadata accessor から渡し、literal 重複の AST 検査を追加。argv shim を `--daemon` 前置き後の head に適用。doc pin の正式名を 9 つにし、`backlog list` と `policy remove` の制約文を pin。`collect_pending_facts` の `settings` / `conn` の `None` 許容、AC-16(f) の逐語化、client の import pin の範囲の明記、B2 を B2a / B2b / B2c に分割、I1 の web-lan merge 依存。AC-4 / 6 / 13 / 16 / 17 / 23 / 26 / 32 / 35〜39 を更新、AC-41〜43 を新設 | 設計レビュー 3 周目 sol、High 4 / Medium 4 / Low 4 全採用と前版の未閉鎖 3 点 | |
| 2026-10-24 | v0.5 | `unreadable` / `malformed` を自動上書きしない stale とし、CRLF 照合、手修正案内、起動時 reconcile の fail-open、投影例外の endpoint 境界、byte 基準の `changed`、DB 0 行・file 不在の記録のみ移行、reconcile の autopilot 許可を固定。help の完全形 `usage` / `shell_usage` 分離、整数 tuple の version 比較、色を消した golden を追加し、AC-6 / 13 / 16 / 32 / 35 / 39 / 42 / 43 を更新 | 4 周目の指摘 | |
| 2026-10-24 | v0.5a | AC-13 / AC-32 に ID の食い違う終了 marker 3 行と CRLF の managed block 全体の fixture を逐語で追加。golden test は `color=False` を渡さず環境変数の除去だけにする (3.13 の argparse は `color` を受けない)。`unreadable` の show は手修正の固定文を出す。 | 閉鎖確認 (codex sol) の Medium 2 / Low 1 | |
| 2026-10-24 | v0.5b | golden test は非 TTY capture + 3.14 以上だけ `color=False`。再生成する managed block の行終端は常に LF、外側の byte は保持 (CRLF block → `missing` の golden を追加)。 | 差分再確認 (codex terra) の Medium 2 | |
| 2026-10-24 | v0.5c | §8.2-1 対話 shell の寿命をユーザー裁定で確定 (0.2.0 deprecated 表示、1.0.0 で削除)。 | ユーザー承認 | |
| 2026-10-24 | v0.5d | §2.2 に `reconf` の書換えは web だけと明記。§4.7 に終了コードは表示した行から算出すると明記。 | 閉鎖確認 (opus) の Low 2 / Low 6 | |
| 2026-10-24 | v0.5e | §4.7 に `kind_mismatch` / `kind_missing` の issue 語彙と行の置き方を明記。 | A1 差分再確認 (opus) の Low | |
| 2026-10-24 | v0.5f | §5.2 に has_more の固定行、空文字 last_result の扱い、redactor 8 KiB 上限に対する前段切り詰めを明記。 | B1 /code-review (sonnet) の指摘 2 / 3 / 4 | |
| 2026-10-24 | v0.5g | §5.2 前段切り詰めで空白の無い値は本文を残さない (token 断片が redaction を逃れる経路を閉じる)。 | B1 閉鎖確認 (terra) の High | |
| 2026-10-24 | v0.5h | §5.2 前段切り詰めの区切りを redactor の正規化後も残る空白に限定 (CR 等で token を跨がせる経路を閉じる)。 | B1 閉鎖再確認 (terra) の High | |
| 2026-10-24 | v0.5i | §5.3.3 の marker 除去を「認識済み block の marker 行だけ、行終端は保存、公開 helper を共有」に明確化。 | B2a codex r1 の P1 と /code-review の #4 | |
| 2026-10-24 | v0.5j | §5.3.5 に unmanaged の空行除外と `\r` 除去、表示の畳み方 (199 字 + `…`) を明記。§5.4 に「固定 activity」= `policy_*` 行で、共通の監査投影行 `ops_<endpoint>` は対象外と明記。 | B2b codex r1 (High 2 / 3 は投影行の誤読、Medium 1) と /code-review #4 | |
| 2026-10-25 | v0.5k | §5.3.4 に shell fallback の失敗文 2 種、段 12 / shell が共有する書込み手順 (insert → commit → reconcile → activity、`request_id` は uuid4) と段 12 の DB 接続を先頭で開くことを明記 | B2c 着手前の指揮者による補完 (shell fallback の失敗文が未定義) | |
| 2026-10-25 | v0.5l | §5.3.4 の共有手順を API と同じ順 (先頭 reconcile → 本文検査 4000 字 → insert → reconcile → activity) に改め、activity 失敗は第 2 文にしない、insert_policy の commit 失敗は rollback、段 12 の重複判定に DB 完全一致を追加、DB を開けないときの固定文を新設 | B2c /code-review high 6 件 (#1 activity の誤診断、#2 DB 二重行、#3 connect 失敗で段が落ちる、#4 4000 字の未検査、#5 先頭 reconcile 不在、#6 commit 失敗の rollback 不在) | |
| 2026-10-25 | v0.5m | §5.3.4: activity 失敗は ActivityLog の非送出契約に従い成功扱い (第 2 文にしない)、段 12 の重複判定での OSError / sqlite3.Error は第 1 文 | B2c 閉鎖確認 (terra) P1 2 件: ActivityLog.write は例外を出さないので「伝播」は成立しない / 重複判定の file 読取が先頭 reconcile より前で OSError が未捕捉 | |
| 2026-10-25 | v0.5n | §6.3: `unavailable` の定義と rc の規則 (unavailable のみ rc 1、invalid / unknown / missing は rc 0)、ops の読取例外は `unavailable`、`pending steps: unavailable`、DB 3 行は別読取 | A2 受入 test (sonnet) が見つけた実装との食い違い 7 本の裁定 (4〜6 は spec の語を厳密化) | |
| 2026-10-25 | v0.5o | §6.3 の `mode` の語を実 schema (`Mode` = learning / trading) に合わせ、§6.2 に pending 用の plugin inventory の取り方 (daemon と同じ `approved_plugins`、欠落時は `()`) を追加 | A2 codex r1 High 2 (state の mode 語が実 schema と不一致、plugin inventory 未収集) | |
| 2026-10-25 | v0.5p | §6.2 / 6.3: `is-active` の語を systemd の答えどおり (active / activating / deactivating / inactive / failed、観測できなければ unknown)、activity は field ごとに redact、plugin inventory の構築例外は pending steps unavailable。`data/agentic.db` の regular 判定は symlink を辿る (stat)、辿れない symlink は unavailable | A2 /code-review high 8 件 (#2 #3 #4 #6 採用、#5 #7 #8 部分採用、#1 probe の瞬間 shared lock は §6.1 の設計どおりで却下) | |
| 2026-10-25 | v0.5q | §6.3 の `next` の start 条件に active の語 (inactive / failed / unknown) を加える | A2 是正 2 (sonnet) の指摘: is-active の語を増やしたので start を勧める条件が要る | |
