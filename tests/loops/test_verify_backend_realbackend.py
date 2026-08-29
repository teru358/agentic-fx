"""実 backend (local/claude/codex+llama_swap/codex+chatgpt の 4 構成) を
実際に叩く `verify_backend` の統合テスト。既定スイートからは除外される
(`pyproject.toml` の `addopts`)。人間が `-m realbackend` を明示して実行する
— 課金枠消費の上限は「実測回数の上限」節 (手動ランブック) と同じ
(各構成 3 回以内)。

<!-- precheck 2026-08-23 wave3: T13-B9 -->
<!-- I2/M1 是正 (codex 1周目, verified-codex-round1.md): docstring の
「local/claude/codex」という 3 構成表記は現物 (claude は 0 本) と食い違って
いた (プラン記述欠陥、逐語転写)。claude ケースを追加し、実体に合わせて
4 構成へ表記を修正した。 -->
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentic_fx.config import load_settings
from agentic_fx.core.contracts import SystemClock
from agentic_fx.loops.verify_backend import verify_backend

pytestmark = pytest.mark.realbackend

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings():
    return load_settings(_REPO_ROOT / "config" / "settings.yaml.example")


def _with_codex(settings, *, auth_file: str):
    """`runner.codex.auth_file` だけを上書きした settings (個人設定ファイル
    は変更しない — `verify_backend` 自身の `model_copy` 契約と同じ発想)。"""
    return settings.model_copy(update={"runner": settings.runner.model_copy(
        update={"codex": settings.runner.codex.model_copy(
            update={"auth_file": auth_file})})})


def test_verify_backend_real_local(tmp_path):
    if shutil.which("curl") is None:
        pytest.skip("curl not available to probe llama-swap")
    result = verify_backend(tmp_path, _settings(), backend="local",
                           provider=None, clock=SystemClock())
    assert result.ok is True, result.detail


def test_verify_backend_real_codex_llama_swap(tmp_path):
    """I2 是正 (codex 1周目 verified-codex-round1.md 是正4 の一部):
    設計 §1.1-2 (「`provider=llama_swap` は空の scratch `CODEX_HOME`
    (auth.json 無し) で起動する」) / §7.2 (「auth 無し codex+llama_swap の
    実 1 ターン」) は auth.json の**不在**を構成として要求している。是正前
    の skip 条件は `~/.codex/auth.json` の**存在**を実行条件にしており、
    設計が指定した構成 (auth 無し) ではこのゲートが一度も回らなかった
    (プラン記述欠陥、`docs/superpowers/plans/2026-08-20-phase2-10-improve-loop.md:26788-26793`
    と文字単位で同一)。skip ガードを capability 検査
    (`runner.codex.bin` が実在するか) へ置換し、auth_file は実在しない
    パスへ明示的に上書きして「auth 無し」を構成として強制する。
    `codex.bin` が `config/settings.yaml.example` のプレースホルダの
    ままである間は skip され続ける (F-4 残件、
    `tmp/plan10-impl/acceptance-task13.md`) — その解消は本 task のスコープ外。"""
    s = _settings()
    if not Path(s.runner.codex.bin).is_file():
        pytest.skip(f"codex バイナリ未設定: {s.runner.codex.bin}")
    s = _with_codex(s, auth_file=str(tmp_path / "definitely-absent-auth.json"))
    result = verify_backend(tmp_path, s, backend="codex",
                           provider="llama_swap", clock=SystemClock())
    assert result.ok is True, result.detail


def test_verify_backend_real_codex_chatgpt(tmp_path):
    if not (Path("~/.codex/auth.json").expanduser().exists()):
        pytest.skip("~/.codex/auth.json not present")
    result = verify_backend(tmp_path, _settings(), backend="codex",
                           provider="chatgpt", clock=SystemClock())
    assert result.ok is True, result.detail


def test_verify_backend_real_claude(tmp_path):
    """M1 是正 (codex 1周目 verified-codex-round1.md): 設計 §7.2「3 backend
    それぞれで」の claude 脚が現物に無かった (プラン記述欠陥、docstring だけ
    3 構成を謳っていた)。CLI と資格情報が揃っているときだけ走る capability
    skip を置く — `acceptance-task13.md:340` が指摘する「既定の相対
    `runner.claude.bin: claude` が解決されないと課金枠を無駄に焼く」ため。
    claude の過去実測成功記録はリポジトリのどこにも存在しない (一次記録の
    はずの `task13-real-backend-runbook.md` が消失、全数確認済み — 裏取り
    doc 参照) ため、これは新規追加であり過去実測の再利用ではない。"""
    s = _settings()
    if shutil.which(s.runner.claude.bin) is None:
        pytest.skip(f"claude CLI 未解決: {s.runner.claude.bin}")
    if not Path(s.runner.claude.credentials_file).expanduser().is_file():
        pytest.skip("claude credentials 不在")
    result = verify_backend(tmp_path, s, backend="claude", provider=None,
                           clock=SystemClock())
    assert result.ok is True, result.detail
    assert result.provider is None  # I1 の負の脚と同じ観測点
    assert len(result.fingerprint) == 64
