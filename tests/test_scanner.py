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
    from app import config, market
    monkeypatch.setattr(config, "TREND_ENABLED", True)
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


def test_reversion_hold_days_config_parity():
    """Wartość time-stopu dla rewersji musi pochodzić z jednego źródła prawdy w config."""
    from app import config, execution, reversion
    assert reversion.HOLD_DAYS == config.REVERSION_HOLD_DAYS
    assert execution.MAX_HOLD_BARS["reversion"] == config.REVERSION_HOLD_DAYS


def test_trend_disabled_blocks_new_buy(monkeypatch):
    """Gdy TREND_ENABLED=False, nowe sygnały BUY trendu są blokowane."""
    from app import config
    monkeypatch.setattr(config, "TREND_ENABLED", False)

    analysis = {
        "symbol": "PKN.WA",
        "score": 50.0,
        "price": 60.0,
        "liquidity_ok": True,
        "reasons": ["strong momentum"],
        "components": {},
        "rsi": 55.0,
        "adv": 5_000_000,
    }

    sig = scanner._generate_signal(analysis, holding=False)
    assert sig is None
    assert analysis.get("trend_disabled") is True


def test_trend_disabled_allows_sell_exits(monkeypatch):
    """Gdy TREND_ENABLED=False, sygnały SELL zamykające istniejące pozycje trendu nadal działają."""
    from app import config
    monkeypatch.setattr(config, "TREND_ENABLED", False)

    analysis = {
        "symbol": "PKN.WA",
        "score": -35.0,
        "price": 55.0,
        "liquidity_ok": True,
        "reasons": ["trend broken"],
        "components": {},
    }

    sig = scanner._generate_signal(analysis, holding=True)
    assert sig is not None
    assert sig["side"] == "SELL"
    assert sig["strategy"] == "trend-momentum-exit"
    assert sig["meta"]["hypothesis"] == "trend"


def test_trend_disabled_check_stops_closes_stale_lot(monkeypatch):
    """Otwarte transze trendu są poprawnie zamykane przez time-stop nawet przy TREND_ENABLED=False."""
    from app import config, market
    monkeypatch.setattr(config, "TREND_ENABLED", False)
    monkeypatch.setattr(market, "cached_price", lambda s: 100.0)
    monkeypatch.setattr(execution, "bars_held", lambda ts: 25)

    lot_id = db.open_lot(
        symbol="PKN.WA",
        qty=10.0,
        entry_price=100.0,
        entry_fee=1.0,
        stop=80.0,
        target=120.0,
        hypothesis="trend",
    )

    triggered = execution.check_exits()
    assert len(triggered) == 1
    assert triggered[0]["lot_id"] == lot_id
    assert "time-stop" in triggered[0]["trigger"]

    # Lot powinien być zamknięty w bazie
    open_lots = db.open_lots("PKN.WA", "trend")
    assert len(open_lots) == 0


def test_trend_disabled_execute_signals_skips_queued_buy(monkeypatch):
    """Zapisany wcześniej sygnał BUY trendu w execute_signals otrzymuje status SKIPPED."""
    from app import config
    monkeypatch.setattr(config, "TREND_ENABLED", False)

    queued_buy = {
        "ts": int(time.time()),
        "symbol": "ALE.WA",
        "side": "BUY",
        "score": 45.0,
        "price": 30.0,
        "strategy": "trend-momentum-long",
        "meta": {"hypothesis": "trend", "sizing": {"qty": 10.0, "stop": 28.0}},
    }

    res = scanner.execute_signals([queued_buy])
    assert len(res) == 1
    assert res[0]["status"] == "SKIPPED"
    assert res[0]["reason"] == "trend disabled"


def test_reversion_disabled_on_gpw_skips_buy(monkeypatch):
    """Gdy REVERSION_GPW_ENABLED=False, sygnały BUY rewersji dla spółek GPW (.WA) otrzymują status SKIPPED."""
    from app import config
    monkeypatch.setattr(config, "REVERSION_GPW_ENABLED", False)

    queued_buy = {
        "ts": int(time.time()),
        "symbol": "CDR.WA",
        "side": "BUY",
        "score": 60.0,
        "price": 140.0,
        "strategy": "reversion-long",
        "meta": {"hypothesis": "reversion", "sizing": {"qty": 5.0, "stop": 130.0}},
    }

    res = scanner.execute_signals([queued_buy])
    assert len(res) == 1
    assert res[0]["status"] == "SKIPPED"
    assert res[0]["reason"] == "reversion disabled on GPW"


def test_reversion_enabled_on_us_fills_buy(monkeypatch):
    """Sygnały BUY rewersji dla spółek US (np. META) są realizowane bez przeszkód."""
    from app import config, market
    monkeypatch.setattr(config, "REVERSION_GPW_ENABLED", False)
    monkeypatch.setattr(market, "cached_price", lambda s: 500.0)

    queued_buy = {
        "ts": int(time.time()),
        "symbol": "META",
        "side": "BUY",
        "score": 60.0,
        "price": 500.0,
        "strategy": "reversion-long",
        "meta": {"hypothesis": "reversion", "sizing": {"qty": 1.0, "stop": 470.0}},
    }

    res = scanner.execute_signals([queued_buy])
    assert len(res) == 1
    assert res[0]["status"] == "FILLED"
    assert res[0]["symbol"] == "META"

    open_lots = db.open_lots("META", "reversion")
    assert len(open_lots) == 1


def test_reversion_us_slot_limit_enforced(monkeypatch):
    """Gdy liczba otwartych pozycji US reversion osiągnie US_MAX_SLOTS, kolejne wejścia są pomijane."""
    from app import config, market
    monkeypatch.setattr(config, "US_MAX_SLOTS", 2)
    monkeypatch.setattr(config, "REVERSION_GPW_ENABLED", False)
    monkeypatch.setattr(market, "cached_price", lambda s: 100.0)

    # Otwórz 2 pozycje US reversion
    sig1 = {
        "ts": int(time.time()), "symbol": "AAPL", "side": "BUY", "score": 70.0, "price": 100.0,
        "strategy": "reversion-long", "meta": {"hypothesis": "reversion", "sizing": {"qty": 1.0, "stop": 90.0}, "z": -2.5}
    }
    sig2 = {
        "ts": int(time.time()), "symbol": "MSFT", "side": "BUY", "score": 65.0, "price": 100.0,
        "strategy": "reversion-long", "meta": {"hypothesis": "reversion", "sizing": {"qty": 1.0, "stop": 90.0}, "z": -2.2}
    }
    sig3 = {
        "ts": int(time.time()), "symbol": "GOOGL", "side": "BUY", "score": 60.0, "price": 100.0,
        "strategy": "reversion-long", "meta": {"hypothesis": "reversion", "sizing": {"qty": 1.0, "stop": 90.0}, "z": -2.0}
    }

    res = scanner.execute_signals([sig1, sig2, sig3])
    assert len(res) == 3
    # Pierwsze dwa wypełnione, trzeci pominięty z powodu limitu slotów
    assert res[0]["status"] == "FILLED" and res[0]["symbol"] == "AAPL"
    assert res[1]["status"] == "FILLED" and res[1]["symbol"] == "MSFT"
    assert res[2]["status"] == "SKIPPED" and res[2]["symbol"] == "GOOGL"
    assert "max US slots" in res[2]["reason"]


def test_execute_signals_buys_sorted_by_z_score(monkeypatch):
    """Kandydaci BUY dla rewersji są sortowani po najniższym z-score (najbardziej wyprzedane pierwsze)."""
    from app import config, market
    monkeypatch.setattr(config, "US_MAX_SLOTS", 1)
    monkeypatch.setattr(market, "cached_price", lambda s: 100.0)

    # Dwa sygnały: MSFT ma z = -2.1, AAPL ma z = -3.2 (bardziej wyprzedany)
    sig_msft = {
        "ts": int(time.time()), "symbol": "MSFT", "side": "BUY", "score": 60.0, "price": 100.0,
        "strategy": "reversion-long", "meta": {"hypothesis": "reversion", "sizing": {"qty": 1.0, "stop": 90.0}, "z": -2.1}
    }
    sig_aapl = {
        "ts": int(time.time()), "symbol": "AAPL", "side": "BUY", "score": 80.0, "price": 100.0,
        "strategy": "reversion-long", "meta": {"hypothesis": "reversion", "sizing": {"qty": 1.0, "stop": 90.0}, "z": -3.2}
    }

    # Przekazujemy w kolejności [MSFT, AAPL]
    res = scanner.execute_signals([sig_msft, sig_aapl])
    assert len(res) == 2
    # AAPL musi wejść pierwszy jako bardziej wyprzedany, a MSFT zostać pominięty przez limit slotów 1
    assert res[0]["status"] == "FILLED" and res[0]["symbol"] == "AAPL"
    assert res[1]["status"] == "SKIPPED" and res[1]["symbol"] == "MSFT"


