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


def test_history_dataset_15m_width_and_as_dict():
    """段階 2 レビュー是正 C1-1: `_WIDTHS` から "15m" が抜けると
    ``HistoryDataset("mt5", "15m")`` 自体が構築できなくなる (許容一覧の
    縮退) — 15m を明示的に構築できること・width・as_dict をピンする。
    """
    dataset = HistoryDataset("mt5", "15m")
    assert dataset.width == timedelta(minutes=15)
    assert dataset.as_dict() == {"source": "mt5", "base_interval": "15m"}
