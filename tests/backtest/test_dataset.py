from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest

from agentic_fx.backtest.dataset import DatasetError, HistoryDataset


def test_history_dataset_validates_and_exposes_immutable_metadata():
    dataset = HistoryDataset("dukascopy", "5m")
    assert dataset.width == timedelta(minutes=5)
    assert dataset.as_dict() == {"source": "dukascopy", "base_interval": "5m"}
    with pytest.raises(FrozenInstanceError):
        dataset.source = "mt5"


@pytest.mark.parametrize("source,interval", [("bad", "1m"),
                                                ("dukascopy", "2m")])
def test_history_dataset_rejects_unknown_source_or_interval(source, interval):
    with pytest.raises(DatasetError):
        HistoryDataset(source, interval)
