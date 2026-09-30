"""Testy hipotezy rewersji na danych syntetycznych.

Najdroższy błąd w historii tego projektu był cichy: warunek
`z >= 0 or not pd.isna(z)` jest prawdziwy zawsze, więc rewersja wychodziła
natychmiast po wejściu i raportowała fałszywe -99%.

Fixture muszą dawać NIEZEROWE odchylenie i NIEZEROWY ATR. Ciągła seria
kursów daje sd=0 i ATR=0, a wtedy z-score to NaN i test przechodziłby
na fałszywym założeniu. Dlatego budujemy falę, nie prostą.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from app import reversion as rv
from app import signals


def _raw(closes: list[float], spread: float = 0.02) -> pd.DataFrame:
    """Świece z realnym zakresem — ATR musi być niezerowy, inaczej poziomy
    ryzyka są nieokreślone.

    `spread` to szerokość świecy jako ułamek ceny i wprost steruje ATR-em:
    ATR ≈ spread × cena. Od niego zależy, czy podłoga -10% jest wiążąca
    (stop ATR = 2,5 × ATR musi być mniejszy niż 10% ceny).
    """
    n = len(closes)
    # Epoka jako int — tak `ts` wraca z bazy. evaluate() robi int(row["ts"]),
    # więc Timestamp wywołałby TypeError; fixture musi odzwierciedlać produkcję.
    return pd.DataFrame({
        "ts": [1_700_000_000 + i * 86_400 for i in range(n)],
        "open": closes,
        "high": [c * (1 + spread) for c in closes],
        "low": [c * (1 - spread) for c in closes],
        "close": closes,
        "volume": [1_000_000] * n,
    })


def _frame(closes: list[float]) -> pd.DataFrame:
    """Ten sam pipeline, który używa evaluate(): wskaźniki, potem rewersja.
    Pominięcie kroku daje fałszywe testy — rewersja czyta kolumnę `atr`."""
    return rv.compute(signals.compute_indicators(_raw(closes)))


def _wave(n: int, center: float = 100.0, amp: float = 3.0) -> list[float]:
    """Fala sinusoidalna: średnia ~center, odchylenie > 0, zakres > 0."""
    return [center + amp * math.sin(i * 0.7) for i in range(n)]


# --- z-score --------------------------------------------------------------

def test_zscore_is_exactly_zero_at_the_window_mean():
    """Ostatnia cena równa średniej okna musi dać z = 0, nie NaN."""
    wave = _wave(rv.LOOKBACK - 1)
    mean = sum(wave) / len(wave)
    z = _frame(wave + [mean])["rv_z"].dropna()
    assert len(z) > 0
    assert z.iloc[-1] == pytest.approx(0.0, abs=1e-9)


def test_zscore_is_nan_when_the_window_has_no_variation():
    """Seria bez zmienności ma sd=0. Podzielenie przez zero musi dać NaN —
    zerowanie z-score'a było drugą przyczyną tej samej awarii.

    Sprawdzamy TYLKO okno po napełnieniu. `isna().all()` na całej ramce
    przechodziłoby zawsze, bo pierwsze LOOKBACK-1 wierszy to NaN z braku
    historii — test nie odróżniałby „sd=0" od „kolumna w ogóle nie istnieje".
    """
    df = _frame([100.0] * 40)
    warm = df.iloc[rv.LOOKBACK - 1:]
    assert len(warm) > 0, "okno po napełnieniu nie może być puste"
    # twardy warunek: faktycznie liczone odchylenie jest zerowe
    assert (warm["rv_sd"] == 0.0).all(), "fixture musi mieć zerowe odchylenie"
    # i dopiero wtedy z-score musi być NaN, a nie 0.0 ani inf
    assert warm["rv_z"].isna().all()
    assert not np.isinf(warm["rv_z"].to_numpy(dtype="float", na_value=0.0)).any()


def test_zscore_is_nan_before_the_lookback_fills():
    """Zimny start to brak danych, nie dane równe zeru."""
    df = _frame(_wave(30))
    assert df["rv_z"].iloc[:rv.LOOKBACK - 1].isna().all()
    assert df["rv_z"].dropna().notna().any()


def test_price_below_the_mean_gives_negative_z():
    wave = _wave(25)
    z = _frame(wave + [80.0])["rv_z"].dropna().iloc[-1]
    assert z < -rv.Z_ENTRY, \
        f"kurs 20% pod średnią musi przekroczyć próg wejścia, było {z:.2f}"


def test_price_above_the_mean_gives_positive_z():
    wave = _wave(25)
    z = _frame(wave + [120.0])["rv_z"].dropna().iloc[-1]
    assert z > rv.Z_ENTRY


def test_zscore_sign_follows_the_price():
    wave = _wave(25)
    below = _frame(wave + [80.0])["rv_z"].dropna().iloc[-1]
    above = _frame(wave + [120.0])["rv_z"].dropna().iloc[-1]
    assert below < 0 < above


# --- rozciągnięcie i ATR --------------------------------------------------

def test_stretch_in_atr_is_negative_below_the_mean():
    stretch = _frame(_wave(25) + [80.0])["rv_stretch_atr"].dropna().iloc[-1]
    assert stretch < 0


def test_atr_is_positive_on_real_ranges():
    atr = _frame(_wave(25) + [80.0])["atr"].dropna().iloc[-1]
    assert atr > 0


# --- poziomy ryzyka -------------------------------------------------------

def test_evaluate_derives_levels_from_its_own_stop_formula(monkeypatch):
    """Poziomy muszą pochodzić z evaluate(), nie z formuły przepisanej
    w teście. Wersja poprzednia liczyła `max(price - 2.5*atr, price*0.90)`
    we własnym kodzie i przechodziła, nawet gdyby evaluate() zmieniło
    swoją logikę — test niczego nie pilnował.

    ATR 1 przy cenie 100: podłoga -10% nie jest wiążąca, więc relacja
    zysk/ryzyko to dokładnie 3,0/2,5 = 1,2.
    """
    from app import signals as sg
    # Delikatna fala: ATR musi trzymać się poniżej 4% ceny, inaczej podłoga
    # -10% byłaby wiążąca i test nie mierzyłby relacji zysk/ryzyko z ATR,
    # tylko to, że podłoga zadziałała (to osobny test niżej).
    base = _raw(_wave(60, amp=0.3), spread=0.005)
    monkeypatch.setattr(sg, "load_frame", lambda sym, limit=1000: base)
    analysis = rv.evaluate("TEST.WA")
    assert analysis is not None
    price, atr = analysis["price"], analysis["atr"]
    assert atr > 0
    assert price - rv.STOP_ATR * atr > price * 0.90, \
        "fixture musi mieć ATR poniżej progu podłogi -10%"
    assert analysis["stop"] == pytest.approx(price - rv.STOP_ATR * atr, rel=1e-3)
    assert analysis["take"] == pytest.approx(price + rv.TARGET_ATR * atr, rel=1e-3)
    assert (analysis["take"] - price) / (price - analysis["stop"]) == pytest.approx(1.2, rel=0.05)


def test_evaluate_applies_the_ten_percent_capital_floor(monkeypatch):
    """Gdy ATR jest ogromny, 2,5×ATR dałoby stop daleko poza -10% ceny.
    Podłoga ma zadziałać właśnie wtedy — i tylko wtedy."""
    from app import signals as sg
    base = _raw(_wave(60, amp=25.0), spread=0.05)
    monkeypatch.setattr(sg, "load_frame", lambda sym, limit=1000: base)
    analysis = rv.evaluate("TEST.WA")
    assert analysis is not None
    price, atr = analysis["price"], analysis["atr"]
    assert price - rv.STOP_ATR * atr < price * 0.90, \
        "fixture musi mieć ATR na tyle duży, by podłoga zadziałała"
    assert analysis["stop"] == pytest.approx(price * 0.90, rel=1e-6)


def test_stop_uses_atr_not_a_fixed_percentage(monkeypatch):
    """Różne ATR przy tej samej cenie dają różne stopy — to jest cały sens
    liczenia poziomów w ATR zamiast w procentach."""
    from app import signals as sg

    stops = []
    for amp in (2.0, 10.0):
        base = _raw(_wave(60, amp=amp))
        monkeypatch.setattr(sg, "load_frame", lambda sym, limit=1000, _b=base: _b)
        a = rv.evaluate("TEST.WA")
        assert a is not None
        stops.append(a["stop"])
    assert stops[0] != stops[1]


def test_documented_constants_are_what_the_code_uses():
    """Umowa z dokumentacją w Vault. Zmiana któregokolwiek z tych
    parametrów bez aktualizacji notatek musi wywrócić ten test."""
    assert rv.LOOKBACK == 20
    assert rv.Z_ENTRY == 2.0
    assert rv.HOLD_DAYS == 10
    assert rv.STOP_ATR == 2.5
    assert rv.TARGET_ATR == 3.0


# --- wielkość pozycji -----------------------------------------------------

def test_sizing_scales_down_when_the_stop_is_far():
    """Szerszy stop = mniejsza pozycja przy tym samym budżecie ryzyka."""
    tight = {"price": 100.0, "atr": 2.0, "stop": 95.0, "strength": 0.5}
    wide = {"price": 100.0, "atr": 8.0, "stop": 80.0, "strength": 0.5}
    assert rv.size(wide, 10_000.0)["qty"] < rv.size(tight, 10_000.0)["qty"]


def test_sizing_never_exceeds_the_position_cap():
    analysis = {"price": 100.0, "atr": 0.5, "stop": 99.0, "strength": 1.0}
    assert rv.size(analysis, 10_000.0)["notional"] <= 10_000.0 * 0.15 + 1e-6


def test_sizing_is_zero_below_the_minimum_ticket():
    analysis = {"price": 1.0, "atr": 0.01, "stop": 0.99, "strength": 0.1}
    assert rv.size(analysis, 100.0)["qty"] == 0.0


def test_deeper_stretch_sizes_up():
    """Głębsze rozciągnięcie to większa szansa odbicia — mnożnik
    przekonania rośnie, ale w granicach podanych w kodzie."""
    shallow = {"price": 100.0, "atr": 5.0, "stop": 87.5, "strength": 0.3}
    deep = {"price": 100.0, "atr": 5.0, "stop": 87.5, "strength": 1.0}
    assert rv.size(deep, 10_000.0)["qty"] > rv.size(shallow, 10_000.0)["qty"]
