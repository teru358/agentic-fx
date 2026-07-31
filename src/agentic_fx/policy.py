"""policy/directives.md — 末尾 4000 文字注入 + サイズ警告 (設計書 §8)。追記は Phase 2。"""
from __future__ import annotations

from pathlib import Path


class Policy:
    def __init__(self, path: Path) -> None:
        self._path = path

    def tail(self, chars: int = 4000) -> str:
        if not self._path.exists():
            return ""
        return self._path.read_text(encoding="utf-8")[-chars:]

    def size_warning(self, limit_chars: int = 16000) -> str | None:
        if self._path.exists() and \
                len(self._path.read_text(encoding="utf-8")) > limit_chars:
            return (f"警告: {self._path} が {limit_chars} 文字を超えています。"
                    "注入は末尾 4000 文字のみです。手動で整理してください。")
        return None
