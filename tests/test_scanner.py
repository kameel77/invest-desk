"""Testy izolacji hipotez i logiki skanera."""
from __future__ import annotations

import time
import pytest

from app import db, execution, reversion, scanner


def test_held_lots_are_isolated_by_hypothesis():
    """Transza rewersji nie może trafiać do zbioru held_trend ani blokować sygnałów trendu."""
    db.open_lot(
        symbol="CDR.WA",
        qty=10.0,
        entry_price=150.0,
        entry_fee=1.0,
        stop=140.0,
        target=165.0,
        hypothesis="reversion",
    )

    held_trend = {r["symbol"] for r in db.open_lots(hypothesis="trend")}
    held_reversion = {r["symbol"] for r in db.open_lots(hypothesis=reversion.HYPOTHESIS)}

    assert "CDR.WA" in held_reversion
    assert "CDR.WA" not in held_trend


def test_execute_signals_sell_trend_does_not_close_reversion_lot(monkeypatch):
    """Sygnał SELL trendu nie może zamknąć transzy należącej do hipotezy rewersji."""
    lot_id = db.open_lot(
        symbol="CDR.WA",
        qty=10.0,
        entry_price=150.0,
        entry_fee=1.0,
        stop=140.0,
        target=165.0,
        hypothesis="reversion",
    )

    # Mock cached_price to avoid external market call
    from app import market
    monkeypatch.setattr(market, "cached_price", lambda s: 155.0)

    trend_sell_signal = {
        "ts": int(time.time()),
        "symbol": "CDR.WA",
        "side": "SELL",
        "score": -30.0,
        "price": 155.0,
        "strategy": "trend-momentum-exit",
        "meta": {"hypothesis": "trend", "holding": True},
    }

    res = scanner.execute_signals([trend_sell_signal])
    assert len(res) == 1
    assert res[0]["status"] == "REJECTED"
    assert "no open position" in res[0]["reason"]

    # Transza rewersji musi nadal być otwarta
    open_lots = db.open_lots("CDR.WA", "reversion")
    assert len(open_lots) == 1
    assert open_lots[0]["id"] == lot_id


def test_execute_signals_sells_executed_before_buys(monkeypatch):
    """Zlecenia SELL są wykonywane przed BUY, co uwalnia gotówkę na nowe pozycje."""
    from app import market
    monkeypatch.setattr(market, "cached_price", lambda s: 100.0)

    # Ustaw małą ilość gotówki, niewystarczającą na BUY bez uprzedniej sprzedaży
    db.set_cash(10.0)

    # Otwarta transza gotowa do sprzedaży
    db.open_lot(
        symbol="PKO.WA",
        qty=5.0,
        entry_price=90.0,
        entry_fee=1.0,
        stop=85.0,
        target=105.0,
        hypothesis="trend",
    )

    signals = [
        # BUY przekazany na pierwszym miejscu
        {
            "ts": int(time.time()),
            "symbol": "KGH.WA",
            "side": "BUY",
            "score": 40.0,
            "price": 100.0,
            "strategy": "trend-momentum-long",
            "meta": {"hypothesis": "trend", "sizing": {"qty": 2.0, "stop": 95.0}},
        },
        # SELL na drugim miejscu
        {
            "ts": int(time.time()),
            "symbol": "PKO.WA",
            "side": "SELL",
            "score": -30.0,
            "price": 100.0,
            "strategy": "trend-momentum-exit",
            "meta": {"hypothesis": "trend", "holding": True},
        },
    ]

    res = scanner.execute_signals(signals)
    # Wynik: SELL wykonany, cash uwolniony (~500 zł), BUY wypełniony
    sell_res = [r for r in res if r["symbol"] == "PKO.WA"][0]
    buy_res = [r for r in res if r["symbol"] == "KGH.WA"][0]
    assert sell_res["status"] == "FILLED"
    assert buy_res["status"] == "FILLED"


def test_legacy_null_hypothesis_lot_migration():
    """Legacy lots z hypothesis=NULL są bezpiecznie traktowane jako 'trend' i migrowane."""
    conn = db.get_conn()
    conn.execute("DROP TABLE lots")
    conn.execute(
        "CREATE TABLE lots ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "symbol TEXT NOT NULL, "
        "hypothesis TEXT, "
        "opened_ts INTEGER NOT NULL, "
        "qty REAL NOT NULL, "
        "entry_price REAL NOT NULL, "
        "entry_fee REAL DEFAULT 0, "
        "stop_price REAL, "
        "take_price REAL, "
        "entry_meta TEXT, "
        "closed_ts INTEGER, "
        "exit_price REAL, "
        "exit_fee REAL, "
        "exit_reason TEXT, "
        "signal_id INTEGER, "
        "realized_pnl REAL DEFAULT 0)"
    )
    conn.execute(
        "INSERT INTO lots (symbol, hypothesis, opened_ts, qty, entry_price, entry_fee) "
        "VALUES ('LEGACY.WA', NULL, ?, 10, 50.0, 1.0)",
        (int(time.time()),)
    )
    conn.commit()

    # Przed migracją query dla 'trend' już je widzi dzięki filtrowi
    lots = db.open_lots("LEGACY.WA", "trend")
    assert len(lots) == 1
    assert lots[0]["symbol"] == "LEGACY.WA"

    # Wywołanie init_db migruje rekord trwale
    db.init_db()
    row = db.get_conn().execute("SELECT hypothesis FROM lots WHERE symbol='LEGACY.WA'").fetchone()
    assert row["hypothesis"] == "trend"
