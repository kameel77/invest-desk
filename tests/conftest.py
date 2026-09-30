"""Wspólne fixture'y.

Testy działają na tymczasowej bazie SQLite, nigdy na `data/trading.db` —
konto papierowe to jedyny zapis, czy strategia działa, i nie może go
podzielić z testem.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def temp_db(monkeypatch, tmp_path):
    """Każdy test dostaje własną, pustą bazę."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("TD_DATA_DIR", str(tmp_path))

    from app import config, db
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", db_file)
    monkeypatch.setattr(config, "STARTING_CASH", 10_000.0)

    # zerwamy cache połączenia, bo get_conn() trzyma je thread-local
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn
    db.init_db()
    db.set_cash(config.STARTING_CASH)
    yield db_file
    if hasattr(db._local, "conn"):
        db._local.conn.close()
        del db._local.conn


@pytest.fixture
def frozen_prices(monkeypatch):
    """Podmienia odczyt ceny na słownik, żeby testy nie chodziły do sieci."""
    prices: dict[str, float] = {}

    def _get(symbol: str):
        return prices.get(symbol.upper())

    from app import execution, market
    monkeypatch.setattr(market, "cached_price", _get)
    monkeypatch.setattr(execution.market, "cached_price", _get)
    return prices


def make_candles(prices: list[float]) -> "pd.DataFrame":  # noqa: F821
    """Świece dzienne z zadanych zamknięć — do testów funkcji analitycznych."""
    import pandas as pd
    n = len(prices)
    return pd.DataFrame({
        "ts": list(range(1_700_000_000, 1_700_000_000 + n * 86_400, 86_400)),
        "open": prices,
        "high": [p * 1.01 for p in prices],
        "low": [p * 0.99 for p in prices],
        "close": prices,
        "volume": [1_000_000] * n,
    })


_ = os, tempfile
