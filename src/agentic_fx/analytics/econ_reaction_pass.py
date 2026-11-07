"""Bounded cycle pass that snapshots reactions for terminal event slices."""
from __future__ import annotations

import logging
from datetime import datetime

from agentic_fx._safe_error import safe_error_text
from agentic_fx.analytics import econ_reactions
from agentic_fx.store import econ_reaction_values, ohlcv_slices

log = logging.getLogger(__name__)
REACTION_BATCH = 20


class ReactionCursor:
    """Resume point between passes: the last set id handled, None means start over."""

    def __init__(self) -> None:
        self.after: int | None = None


def run_pass(*, read, write, stopped, pair_currencies, pip_size_fn, now: datetime,
             cursor: ReactionCursor | None = None) -> None:
    after = None if cursor is None else cursor.after
    rows = read(lambda conn: econ_reaction_values.pending_sets(
        conn, pair_currencies=pair_currencies, limit=REACTION_BATCH, after=after))
    if cursor is not None and len(rows) < REACTION_BATCH:
        cursor.after = None
    advance = cursor is not None and len(rows) >= REACTION_BATCH
    prev = None
    for item in rows:
        if stopped(): break
        if advance and prev is not None:
            cursor.after = prev
        prev = item['id']
        try:
            slice_rows = read(lambda conn: ohlcv_slices.complete_rows(conn, item['id']))
            existing = item.get('missing_keys')
            if existing is not None:
                currencies = existing
                values = ()
            else:
                currency_list = econ_reactions.event_currencies(item['country'], pair_currencies)
                currencies = [c for c in currency_list if econ_reactions.canonical_pair(c, pair_currencies) == item['pair']]
                try:
                    spread = item['spread_pips']
                    if item['state'] == ohlcv_slices.STATE_COMPLETE and spread is None:
                        log.error("complete event slice %s has no spread", item['id'])
                    values = econ_reactions.window_values(slice_rows or (),
                        event_ts=datetime.fromisoformat(item['ts']),
                        pip_size=(1.0 if item['state'] == ohlcv_slices.STATE_UNRECOVERABLE
                                  else pip_size_fn(item['pair'])), spread_pips=spread,
                        set_state=item['state'])
                except Exception as e:
                    log.error("econ reaction calculation failed for set %s event %s: %s",
                                  item['id'], item['event_id'], safe_error_text(e))
                    continue
            keys = [(item['event_id'], c) for c in currencies]
            result = write(lambda conn, begin, checkpoint: econ_reaction_values.write_set_tx(
                conn, set_id=item['id'], values=values, keys=keys, now=now,
                begin=begin, checkpoint=checkpoint))
            if result is None:
                break
        except Exception as e:
            log.error("econ reaction pass failed for set %s event %s: %s",
                          item['id'], item['event_id'], safe_error_text(e))
    else:
        if advance:
            cursor.after = rows[-1]['id']
