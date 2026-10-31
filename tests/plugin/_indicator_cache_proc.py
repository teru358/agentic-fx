"""Child process for the indicator cache lock tests: builds one entry and holds the key
lock until told to finish."""
from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from agentic_fx.plugin import indicator_fixture_cache as cache


def small_entry() -> cache.CacheEntry:
    idx = pd.date_range("2026-01-05", periods=4, freq="1h", tz="UTC")
    return cache.CacheEntry({"v": pd.Series(np.arange(4.0), index=idx), "s": 1.5},
                            {"v": ("invariant", "invariant")})


def hold_key(data_dir: str, content: str, data: str, tf: str, started: str, release: str,
             plant_tmp: bool) -> None:
    key = cache.CacheKey(content, data, tf)

    def builder() -> cache.CacheEntry:
        if plant_tmp:
            (cache.cache_dir(Path(data_dir)) / f"{key.stem}.tmp-1-ab").write_bytes(b"in progress")
        Path(started).write_text("1")
        deadline = time.monotonic() + 60
        while not Path(release).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return small_entry()

    cache.get_or_build(Path(data_dir), key, builder)
