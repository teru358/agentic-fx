# agentic-fx

決定論的な資金保護コアと、取引判断・改善の二つの agent loop を組み合わせた FX 自動トレードシステムです。まずはペーパー運用で、設定と未完の手順を確認しながら育てます。

前提は Ubuntu 22 以降、Python 3.12 と uv、systemd user session、Node.js と `claude` または `codex` CLI、または llama-swap です。

```bash
git clone <repository-url> agentic-fx
cd agentic-fx
uv sync
uv run afx init
```

`afx init` が設定、user unit の設置、起動後確認までを案内します。やり直すときは `scripts/first-run-reset.sh` を使ってください。

日常の確認は `afx ctl status` から始めます。操作 API の運用は [操作 API と afx ctl の運用](docs/operations/ops-api-2026-10-09.md) を参照してください。

設計は [全体設計](docs/superpowers/specs/2026-07-25-agentic-fx-design.md) と [初期設定ウィザード設計](docs/superpowers/specs/2026-10-12-first-run-wizard-design.md) にあります。秘密は `.env` のみに置きます。Anthropic API の従量課金は使いません。
