"""Bounded cycle pass that snapshots reactions for terminal event slices."""
from __future__ import annotations

import logging
from datetime import datetime

from agentic_fx.analytics import econ_reactions
from agentic_fx.store import econ_reaction_values, ohlcv_slices

log = logging.getLogger(__name__)
REACTION_BATCH = 20


def run_pass(*, read, write, stopped, pair_currencies, pip_size_fn, now: datetime) -> None:
    rows = read(lambda conn: econ_reaction_values.pending_sets(
        conn, pair_currencies=pair_currencies, limit=REACTION_BATCH))
    for item in rows:
        if stopped(): break
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
                except Exception:
                    log.exception("econ reaction calculation failed for set %s", item['id'])
                    continue
            keys = [(item['event_id'], c) for c in currencies]
            result = write(lambda conn, begin, checkpoint: econ_reaction_values.write_set_tx(
                conn, set_id=item['id'], values=values, keys=keys, now=now,
                begin=begin, checkpoint=checkpoint))
            if result is None:
                break
        except Exception:
            log.exception("econ reaction pass failed for set %s", item['id'])
