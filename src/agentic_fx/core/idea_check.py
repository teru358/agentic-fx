"""改善課題の文面の共通検査 (shell の `improve add` と初期設定が同じ規則を使う)。"""
from __future__ import annotations

import unicodedata


def normalize_idea_display(text: str) -> tuple[str, int]:
    """表示と警告判定用に文字列を正規化する。戻り値は (正規化後の文字列, 取り除いた文字数)。"""
    step1 = text.replace("\n", " ")
    kept = [ch for ch in step1 if not unicodedata.category(ch).startswith("C")]
    removed = len(step1) - len(kept)
    return "".join(kept).strip(), removed


def idea_looks_unintended(display: str) -> bool:
    """プレースホルダ (`<…>` / `[…]`) または短すぎる (4 文字未満) 文面か。"""
    is_placeholder = ((display.startswith("<") and display.endswith(">"))
                      or (display.startswith("[") and display.endswith("]")))
    return is_placeholder or len(display) < 4
