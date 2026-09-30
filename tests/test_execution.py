"""Testy arytmetyki transzy.

Ten moduł liczy pieniądze. Każdy błąd tutaj jest cichy — system nadal
działa, tylko mówi złe liczby — więc testy celują w konkretne klasy błędów,
które już wystąpiły, a nie w pokrycie linii.
"""
from __future__ import annotations

import pytest

from app import config, db, execution


# --- wypełnienie i koszty -------------------------------------------------

def test_fill_pays_up_on_buy_and_down_on_sell():
    """Kupujący płaci więcej, sprzedający dostaje mniej — inaczej system
    zyskuje na każdej transakcji z samego spreadu."""
    up = execution._fill_price(100.0, "BUY")
    down = execution._fill_price(100.0, "SELL")
    assert up > 100.0
    assert down < 100.0


def test_commission_respects_percentage_and_minimum():
    assert execution._commission(10_000.0) == pytest.approx(35.0)
    # poniżej progu minimalnego obowiązuje minimum
    assert execution._commission(10.0) == pytest.approx(config.COMMISSION_MIN)


def test_commission_is_zero_for_empty_notional():
    assert execution._commission(0.0) == 0.0


# --- poziomy ryzyka -------------------------------------------------------

def test_levels_are_measured_from_the_entry_not_the_live_price(frozen_prices,
                                                                monkeypatch):
    """Regresja: poziomy liczone z bieżącej ceny dały stop POWYŻEJ ceny
    wejścia transzy, który natychmiast się wybijał. Wejście po limit price
    poniżej ceny rynkowej jest dokładnie tą sytuacją."""
    from app import reversion as rv
    # ATR 10 przy cenie rynkowej 100. Gdyby poziomy liczono z ceny rynkowej,
    # stop wyszedłby 75 — powyżej wejścia 80 i transza otworzyłaby się
    # naruszona. Liczony z wejścia daje 80 - 25 = 55.
    monkeypatch.setattr(rv, "evaluate", lambda s: {
        "symbol": s.upper(), "price": 100.0, "atr": 10.0,
        "stop": 75.0, "take": 130.0, "eligible": True, "strength": 0.6,
        "z": -2.5, "ma": 100.0, "mean_gap_pct": 0.0, "stretch_atr": -3.0,
        "target_mean": 100.0, "max_hold_days": 10, "reasons": [], "ts": 0,
    })
    frozen_prices["XYZ.WA"] = 100.0
    res = execution.buy("XYZ.WA", 1.0, hypothesis="reversion",
                        limit_price=80.0, stop=None, target=None)
    assert res["status"] == "FILLED"
    assert res["price"] < 100.0            # wejście poniżej rynku
    # fill zawiera slippage, więc poziomy liczymy od niego, nie od limit price
    assert res["stop"] == pytest.approx(res["price"] - 2.5 * 10.0, rel=1e-3)
    assert res["stop"] < res["price"]
    assert res["target"] == pytest.approx(res["price"] + 3.0 * 10.0, rel=1e-3)


def test_stop_above_entry_is_clamped_down(frozen_prices):
    """Jeśli caller poda stop powyżej ceny, wejście nie może otworzyć się
    już naruszone."""
    frozen_prices["AAA.WA"] = 100.0
    res = execution.buy("AAA.WA", 1.0, hypothesis="reversion",
                        stop=150.0, target=None)
    assert res["status"] == "FILLED"
    assert res["stop"] < res["price"]


def test_target_below_entry_is_clamped_up(frozen_prices):
    frozen_prices["AAA.WA"] = 100.0
    res = execution.buy("AAA.WA", 1.0, hypothesis="reversion",
                        stop=None, target=50.0)
    assert res["status"] == "FILLED"
    assert res["target"] > res["price"]


# --- transze --------------------------------------------------------------

def test_two_entries_same_symbol_stay_separate(frozen_prices):
    """Kluczowa własność: ponowne wejście NIE uśrednia się z pierwszym."""
    frozen_prices["BBB.WA"] = 100.0
    a = execution.buy("BBB.WA", 1.0, hypothesis="reversion")
    b = execution.buy("BBB.WA", 1.0, hypothesis="reversion")

    assert a["lot_id"] != b["lot_id"]
    lots = db.open_lots("BBB.WA")
    assert len(lots) == 2
    assert len({l["entry_price"] for l in lots}) == 1  # ta sama cena wejścia


def test_each_lot_keeps_its_own_pnl(frozen_prices):
    """Wejścia w różnych cenach dają różne wyniki — tego nie da się odtworzyć
    z uśrednionej pozycji. Wejście po 100 i po 80, cena wraca do 90:
    pierwsze transzy jest stratą, drugie zyskiem."""
    frozen_prices["CCC.WA"] = 100.0
    a = execution.buy("CCC.WA", 10.0, hypothesis="reversion")
    frozen_prices["CCC.WA"] = 80.0
    b = execution.buy("CCC.WA", 10.0, hypothesis="reversion")
    frozen_prices["CCC.WA"] = 90.0

    views = {l["id"]: execution.lot_view(l) for l in db.open_lots("CCC.WA")}
    assert views[a["lot_id"]]["unrealized_pnl"] < 0   # wejście za drogo
    assert views[b["lot_id"]]["unrealized_pnl"] > 0   # wejście w dołku

    position = execution.equity()["positions"][0]
    # uśredniona pozycja zgadza się co do znaku tam, gdzie transza wygrana
    # i przegrana się uśredniają — dokładnie ten problem, który rozwiązał
    # model transz.
    assert position["lots"] == 2
    assert abs(position["unrealized_pct"]) < abs(views[b["lot_id"]]["unrealized_pct"])


def test_selling_one_lot_leaves_the_others_open(frozen_prices):
    frozen_prices["DDD.WA"] = 100.0
    a = execution.buy("DDD.WA", 1.0, hypothesis="reversion")
    execution.buy("DDD.WA", 1.0, hypothesis="reversion")

    execution.sell_lot(a["lot_id"], reason="test")
    assert len(db.open_lots("DDD.WA")) == 1


def test_splitting_a_lot_is_refused(frozen_prices):
    """Transza była decyzją jako całość — połowa transzy nie ma sensu."""
    frozen_prices["EEE.WA"] = 100.0
    execution.buy("EEE.WA", 10.0, hypothesis="reversion")
    res = execution.sell("EEE.WA", qty=4.0)
    assert res["status"] == "REJECTED"
    assert "split" in res["reason"].lower()
    assert len(db.open_lots("EEE.WA")) == 1


def test_sell_without_position_is_rejected(frozen_prices):
    assert execution.sell("ZZZ.WA")["status"] == "REJECTED"


def test_buy_rejected_without_price(frozen_prices):
    assert execution.buy("NOWE.WA", 1.0)["status"] == "REJECTED"


# --- reguły wyjścia --------------------------------------------------------

def test_stop_loss_closes_the_lot(frozen_prices):
    frozen_prices["FFF.WA"] = 100.0
    lot = execution.buy("FFF.WA", 1.0, hypothesis="reversion", stop=95.0, target=110.0)
    frozen_prices["FFF.WA"] = 94.0
    execution.check_exits()
    row = db.get_conn().execute("SELECT * FROM lots WHERE id=?",
                                (lot["lot_id"],)).fetchone()
    assert row["closed_ts"] is not None
    assert row["exit_reason"] == "stop-loss"


def test_take_profit_closes_the_lot(frozen_prices):
    frozen_prices["GGG.WA"] = 100.0
    lot = execution.buy("GGG.WA", 1.0, hypothesis="reversion", stop=90.0, target=110.0)
    frozen_prices["GGG.WA"] = 111.0
    execution.check_exits()
    row = db.get_conn().execute("SELECT * FROM lots WHERE id=?",
                                (lot["lot_id"],)).fetchone()
    assert row["exit_reason"] == "take-profit"


def test_exit_pnl_net_of_both_fees(frozen_prices):
    """Realizowany P&L musi uwzględniać prowizję wejścia i wyjścia."""
    frozen_prices["HHH.WA"] = 100.0
    lot = execution.buy("HHH.WA", 10.0, hypothesis="reversion", stop=90.0, target=110.0)
    frozen_prices["HHH.WA"] = 110.0
    execution.check_exits()

    row = db.get_conn().execute("SELECT * FROM lots WHERE id=?",
                                (lot["lot_id"],)).fetchone()
    gross = (row["exit_price"] - row["entry_price"]) * row["qty"]
    assert row["realized_pnl"] < gross          # koszty zjedły część zysku
    assert row["realized_pnl"] > gross - 20.0   # ale nie zjadły wszystkiego


def test_time_stop_closes_a_lot_that_outlived_its_window(frozen_prices):
    """10 świec rewersji, 20 trendu — bez tego strata czeka w nieskończoność."""
    frozen_prices["III.WA"] = 100.0
    lot = execution.buy("III.WA", 1.0, hypothesis="reversion",
                        stop=50.0, target=200.0)
    conn = db.get_conn()
    with db.tx() as c:
        c.execute("UPDATE lots SET opened_ts = opened_ts - ? WHERE id=?",
                  (200 * 86_400, lot["lot_id"]))  # ~200 dni kalendarzowych
    assert conn is not None
    execution.check_exits()
    row = db.get_conn().execute("SELECT * FROM lots WHERE id=?",
                                (lot["lot_id"],)).fetchone()
    assert row["closed_ts"] is not None
    assert "time-stop" in row["exit_reason"]


# --- spójność konta -------------------------------------------------------

def test_equity_equals_cash_plus_positions(frozen_prices):
    frozen_prices["JJJ.WA"] = 100.0
    execution.buy("JJJ.WA", 5.0, hypothesis="reversion")
    frozen_prices["JJJ.WA"] = 110.0

    eq = execution.equity()
    assert eq["equity"] == pytest.approx(eq["cash"] + eq["market_value"], rel=1e-6)


def test_cash_never_goes_negative(frozen_prices):
    frozen_prices["KKK.WA"] = 100.0
    res = execution.buy("KKK.WA", 10_000.0)  # 1 mln przy 10 tys. kapitału
    assert res["status"] == "REJECTED"
    assert db.get_cash() == pytest.approx(config.STARTING_CASH)
    assert db.open_lots() == []


def test_realized_pnl_survives_closing_the_position(frozen_prices):
    frozen_prices["LLL.WA"] = 100.0
    execution.buy("LLL.WA", 10.0, hypothesis="reversion", stop=90.0, target=110.0)
    frozen_prices["LLL.WA"] = 110.0
    execution.check_exits()

    eq = execution.equity()
    assert eq["open_positions"] == 0
    assert eq["realized_pnl"] > 0
    assert eq["equity"] == pytest.approx(config.STARTING_CASH + eq["realized_pnl"],
                                         rel=1e-3)


# --- reset ----------------------------------------------------------------

def test_reset_archives_before_wiping(frozen_prices, tmp_path):
    """Regresja: reset kasował historię bez kopii.

    Regresja #2: ścieżka archiwum była względna do CWD (`Path("data")`),
    więc testy zaścigały produkcyjne `data/` własnymi archiwami, a start
    aplikacji z innego katalogu zapisałby kopię gdzie indziej. Archiwum
    musi trafić do DATA_DIR i tylko tam.
    """
    import shutil
    from pathlib import Path

    from app import config as cfg
    frozen_prices["MMM.WA"] = 100.0
    execution.buy("MMM.WA", 1.0, hypothesis="reversion")

    original, original_dir = cfg.DB_PATH, cfg.DATA_DIR
    sandbox = tmp_path / "sandbox"
    sandbox.mkdir()
    cfg.DATA_DIR = sandbox
    cfg.DB_PATH = sandbox / "acct.db"
    # reset() archiwizuje tylko jeśli plik bazy istnieje — a w pierwotnej
    # wersji testu warunek `if res["archive"]` przechodził próżno, gdy
    # archiwum było None. Kopia zapewnia, że faktycznie coś archiwizujemy.
    shutil.copy2(original, cfg.DB_PATH)
    try:
        res = execution.reset()
        assert res["status"] == "RESET"
        assert res["archive"], "reset bez archiwum kasuje historię bez kopii"
        assert Path(res["archive"]).exists()
        assert Path(res["archive"]).parent == sandbox, \
            "archiwum musi trafić do DATA_DIR, nie do katalogu roboczego"
    finally:
        cfg.DB_PATH, cfg.DATA_DIR = original, original_dir


def test_reset_spares_the_decision_journal(frozen_prices):
    frozen_prices["NNN.WA"] = 100.0
    execution.buy("NNN.WA", 1.0, hypothesis="reversion")
    before = db.get_conn().execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
    assert before > 0

    execution.reset()
    after = db.get_conn().execute("SELECT COUNT(*) n FROM decisions").fetchone()["n"]
    assert after == before, "dziennik decyzji musi przeżyć reset"


# --- dziennik decyzji -----------------------------------------------------

def test_journal_records_a_filled_entry(frozen_prices):
    frozen_prices["OOO.WA"] = 100.0
    execution.buy("OOO.WA", 1.0, hypothesis="reversion", reason="test")
    rows = db.recent_decisions(action="FILLED")
    assert rows
    assert rows[0]["symbol"] == "OOO.WA"
    assert rows[0]["hypothesis"] == "reversion"


def test_journal_records_a_rejection_with_the_reason(frozen_prices):
    """Odrzucone decyzje są równie ważne jak wykonane — bez nich dziennik
    kłamie o tym, co system myślał."""
    frozen_prices["PPP.WA"] = 100.0
    execution.buy("PPP.WA", 10_000.0)  # za dużo gotówki
    rows = db.recent_decisions(action="REJECTED")
    assert rows
    assert "gotówki" in rows[0]["reason"]


def test_journal_records_a_rejected_split(frozen_prices):
    frozen_prices["QQQ.WA"] = 100.0
    execution.buy("QQQ.WA", 10.0, hypothesis="reversion")
    execution.sell("QQQ.WA", qty=1.0)
    rows = db.recent_decisions(action="REJECTED")
    assert any("podzielić" in r["reason"] for r in rows)
