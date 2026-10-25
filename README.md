# agentic-fx

決定論的な資金保護コアと、取引判断・改善の二つの agent loop を組み合わせた FX 自動トレードシステムです。まずはペーパー運用で、設定と未完の手順を確認しながら育てます。

停止中の設定・記録を診断するときは `afx status`、稼働中 daemon の状態と未完の手順を見るときは `afx ctl status` を使います。daemon の生きた状態が必要かで入口を選ぶ全体の規則は、[CLI の置き場の原則](docs/operations/ops-api-2026-10-09.md#cli-placement-principle)を参照してください。

前提は Ubuntu 22 以降、Python 3.12 と uv、systemd user session、Node.js と `claude` または `codex` CLI、または llama-swap です。改善ループの隔離には Linux 6.18 以降 (Landlock ABI 8) が必要です。

```bash
git clone <repository-url> agentic-fx
cd agentic-fx
uv sync
uv run afx init
```

`afx init` が設定、user unit の設置、起動後確認までを案内します。やり直すときは `scripts/first-run-reset.sh` を使ってください。

どのディレクトリからも `afx` を打てるようにするには、clone 先で `uv tool install --editable <clone 先のパス>` を実行します (`~/.local/bin/afx` ができます。PATH に無いときは `uv tool update-shell`)。bash の補完は `~/.bashrc` に `eval "$(afx completion bash)"` を足すと効きます。作業 root は `--root <dir>`、環境変数 `AFX_ROOT`、今いる場所とその親の `config/settings.yaml` の順で決まります。詳しくは [初回起動の手順書](docs/operations/first-run-acceptance.md) を参照してください。

日常の確認は `afx ctl status` から始めます。操作 API の運用は [操作 API と afx ctl の運用](docs/operations/ops-api-2026-10-09.md) を参照してください。

設計は [全体設計](docs/superpowers/specs/2026-07-25-agentic-fx-design.md) と [初期設定ウィザード設計](docs/superpowers/specs/2026-10-12-first-run-wizard-design.md) にあります。秘密は `.env` のみに置きます。Anthropic API の従量課金は使いません。
