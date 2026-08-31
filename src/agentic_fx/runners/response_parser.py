"""JSON 修復 (前身 response_parser の移植): think 除去・フェンス剥がし・波括弧抽出。"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator


class ParseError(ValueError):
    pass


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _iter_balanced_objects(text: str) -> Iterator[str]:
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if esc:
                esc = False
                continue
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        yield text[start:i + 1]
                        start = text.find("{", i + 1)
                        break
        else:
            start = text.find("{", start + 1)


def _first_balanced_object(text: str) -> str | None:
    return next(iter(_iter_balanced_objects(text)), None)


def parse_json_output(text: str, *,
                      prefer_keys: frozenset[str] | None = None) -> dict:
    cleaned = _THINK_RE.sub("", text)

    # Fix 1: Discard everything from unclosed <think> to end
    # (prevents decoy JSON inside unclosed think from being adopted)
    think_pos = cleaned.find("<think>")
    if think_pos != -1:
        cleaned = cleaned[:think_pos]

    fence = _FENCE_RE.search(cleaned)
    if fence:
        cleaned = fence.group(1)

    # Try cleaned.strip() first (pure JSON case)
    stripped = cleaned.strip()
    if stripped:
        try:
            obj = json.loads(stripped)
            # Fix 2: Reject top-level non-dict (arrays, scalars, etc)
            if isinstance(obj, dict):
                return obj
            # Valid JSON but not dict -> raise immediately, don't fall back
            raise ParseError(f"expected dict, got {type(obj).__name__}: {stripped[:100]!r}")
        except json.JSONDecodeError:
            # Not valid JSON at all, try balanced extraction
            pass

    # Try balanced object extraction (prose-embedded case)
    if prefer_keys is None:
        candidate = _first_balanced_object(cleaned)
        if candidate:
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                pass
    else:
        fallback = None
        for index, candidate in enumerate(_iter_balanced_objects(cleaned)):
            if index == 64:
                break
            try:
                obj = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if fallback is None:
                fallback = obj
            if prefer_keys <= obj.keys():
                return obj
        if fallback is not None:
            return fallback

    raise ParseError(f"no parsable JSON object in: {text[:200]!r}")
