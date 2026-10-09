# Plan Działania: Faza 0 (Bramka Walidacyjna Kosztów i Brokerów US) oraz Faza 1 (Portfolio Engine dla US Reversion)

Data: 2026-10-09
Status: Zatwierdzony po recenzji Claude Opus 5.5

## 1. Kontekst i Cel
W poprzednim etapie ustaliliśmy, że strategia Mean-Reversion na GPW przynosi straty we wszystkich badanych wariantach (-0,24% do -0,81%) ze względu na wysokie prowizje polskiego brokera (0,35% + 0,35%) i poślizg (~0,9% round-trip). Z kolei na rynku US zanotowano wstępne dodatnie expectancy (+0,73% w bazie do +1,35% w sąsiednich wariantach).

Zgodnie z recenzją Claude Opus 5.5, **przewaga na US wymaga rygorystycznej bramki walidacyjnej (Faza 0) przed budową silnika portfelowego (Faza 1)**:
1. Koszty brokerów muszą być modelowane kwotowo (z prowizjami minimalnymi) oraz z uwzględnieniem narzutu FX (np. XTB z kontem w PLN vs IBKR z kontem w USD).
2. Transakcje na rynkach US występują w klastrach (paniki rynkowe) – bootstrap musi być blokowy (po tygodniach/miesiącach dat wejścia), a nie i.i.d.
3. Obowiązuje test uzgodnienia: logika wejść i wyjść silnika portfelowego musi być w 100% tożsama z `reversion.py::simulate()`.

---

## 2. Kryteria Bramki Fazy 0 (Zapisane przed uruchomieniem)
Bramka jest testowana na **sztywnych parametrach bazowych**:
- `lookback = 20`, `z_entry = 2.0`, `hold = 10`, `stop_atr = 2.5`, `target_atr = 3.0`, `min_stretch_atr = 1.0`.

Kryteria przejścia bramki (GO dla Fazy 1):
1. Test przeprowadzony dla zadeklarowanego profilu brokera w `BROKER_PROFILE_US` (domyślnie `IBKR_TIERED_USD` lub `XTB_PLN`).
2. Dolna granica 90% przedziału ufności (90% CI) z **block bootstrapu** (bloki tygodniowe) > +0,10% (podniesione z uwagi na survivorship bias w statycznej watchliście).
3. Co najmniej **5 z 8 fałdów OOS zyskownych** (przy czym fałdy z mniej niż 25 transakcjami liczone są jako niezyskowne).
4. Punktowe expectancy OOS w teście warunków skrajnych (`CONSERVATIVE_US`: 0,10% prowizji, 0,10% poślizgu, 0,5% FX per side) >= 0,0%.

*Jeśli bramka nie zostanie spełniona, następuje NO-GO (wstrzymanie budowy silnika portfela dla tej strategii i rewizja założeń badawczych).*

---

## 3. Szczegółowy Zakres Prac i Architektura

### Krok 1: Kwotowy i procentowy model kosztów w `app/costs.py`
1. Definicja struktury profili brokerskich:
   - `IBKR_TIERED_USD`:
     - prowizja: $0.0035 / akcję (min $0.35, max 1.0% notional) + $0.001 / akcję opłat giełdowych/regulacyjnych.
     - poślizg: 0.05% dla US large-cap / ETF (0.10% dla pozostałych).
     - narzut FX per trade: 0.0% (rachunek walutowy w USD).
   - `XTB_PLN`:
     - prowizja: 0.0% (do 100k EUR obrotu/mies.).
     - poślizg: 0.05%.
     - narzut FX: 0.50% przy kupnie + 0.50% przy sprzedaży (1.0% round-trip).
   - `GPW_RETAIL`:
     - prowizja: 0.35% (min 3.00 PLN).
     - poślizg: 0.10%.
     - narzut FX: 0.0%.
   - `CONSERVATIVE_US`:
     - prowizja: 0.10% (min $1.00).
     - poślizg: 0.10%.
     - narzut FX: 0.50% per side (1.0% round-trip).
2. Funkcje kalkulacji kosztów:
   - `calculate_trade_costs(side, price, shares, profile_name)` -> kwotowe koszty (prowizja, poślizg, fx).
   - `net_return_pct(...)` -> zwrot z uwzględnieniem notional/shares i profilu brokera.
3. Konfiguracja w `app/config.py`:
   - `DEFAULT_BROKER_PROFILE_US = "IBKR_TIERED_USD"` (lub `"XTB_PLN"`).
   - `DEFAULT_BROKER_PROFILE_GPW = "GPW_RETAIL"`.

### Krok 2: Ujednolicenie wspólnej logiki sygnałów i wyjść w `app/reversion.py`
Wydzielenie czystych funkcji logicznych wielokrotnego użytku:
- `evaluate_bar(...)`: sprawdzenie warunku wejścia (`z <= -z_entry` oraz `stretch_atr >= min_stretch_atr`).
- `check_exits(bar, entry_px, stop_px, target_px, bars_held, max_hold)`:
  1. Stop-loss: `min(stop_px, bar['open'])` jeśli `bar['low'] <= stop_px`.
  2. Target ATR: `max(target_px, bar['open'])` jeśli `bar['high'] >= target_px`.
  3. Mean exit: wyjście na `bar['close']` (oraz wariant na `next_bar['open']`) gdy `z >= 0`.
  4. Time stop: wyjście na `bar['close']` gdy `bars_held >= max_hold`.
  Kolejność wyjść ściśle zgodna z `reversion.py::simulate()`.

### Krok 3: Faza 0 - Narzędzie Bramki Walidacyjnej w `tools/run_hypotheses.py`
- Implementacja trybu `--market us --gate`:
  - Uruchomienie 8-fałdowego walk-forward OOS wyłącznie na instrumentach US dla parametrów bazowych.
  - Ewaluacja pod profilami: `IBKR_TIERED_USD`, `XTB_PLN`, `CONSERVATIVE_US`.
  - Obliczenie block bootstrapu (tygodniowe bloki transakcji, 2000 iteracji) i 90% przedziału ufności.
  - Wyliczenie t-stat, liczby zyskownych fałdów (z warunkiem N >= 25 transakcji/fałd).
  - Wyświetlenie werdyktu bramki (GO / NO-GO).

### Krok 4: Faza 1 - Silnik Portfelowy `app/portfolio_sim.py`
1. Architektura pętli zdarzeń:
   - Bar-by-bar na połączonych sesjach giełdowych US.
   - Krok 1 (Początek dnia t+1): Realizacja wejść zakolejkowanych na Close(t) po cenie Open(t+1) + slippage.
   - Krok 2 (W ciągu dnia t+1): Sprawdzenie wyjść dla otwartych pozycji (Stop-loss, Target ATR, wyjście na Close przy z>=0 lub time stop). Zwolnione sloty stają się dostępne do nowej alokacji.
   - Krok 3 (Koniec dnia t+1): Analiza sygnałów wejścia na Close(t+1).
     - Ranking kandydatów: sortowanie rosnąco po `z_score`, tie-breaker alfabetycznie po `symbol`.
     - Rezerwacja wolnych slotów (max N slotów, np. 5 slotów po 20% kapitału).
     - Rejestracja sygnałów odrzuconych (`slot_full`, `cash_exhausted`).
   - Krok 4: Rejestracja wyceny portfela `equity(t+1)`.
2. Metryki portfela:
   - CAGR, Max Drawdown (MDD) i czas trwania obsunięcia (bars under water).
   - Annualized Sharpe Ratio (dla rf=0% oraz rf=4%) liczony z dziennych zmian equity.
   - Sortino Ratio, Calmar Ratio.
   - Slot utilization (% dni z wykorzystaniem >= 80% slotów).
   - Porównanie z benchmarkiem: SPY Buy & Hold.

### Krok 5: Narzędzie CLI i raportowanie
- `tools/run_portfolio_sim.py`: uruchamianie symulacji portfelowej, zapis do `reports/portfolio_us_sim_<timestamp>.json`.
- Wykres / tabela podsumowująca rok po roku.

### Krok 6: Testy jednostkowe i integracyjne
1. `tests/test_costs.py`:
   - Test kwotowych prowizji IBKR (min $0.35, stawka per share, cap 1.0%).
   - Test narzutu FX w XTB (1.0% round-trip) vs 0% w IBKR USD.
   - Test prowizji minimalnej na GPW (3 PLN).
2. `tests/test_portfolio_sim.py`:
   - **Test uzgodnienia (reconciliation)**: przy `slots=∞` i nieograniczonej gotówce wyniki portfela są tożsame z pojedynczymi przebiegami `reversion.simulate()`.
   - Test pierwszeństwa z-score i tie-breakera po symbolu.
   - Test luki cenowej poniżej stopu (egzekucja po Open).
   - Test braku lookahead bias (wejście wyłącznie po Open(t+1) po sygnale z Close(t)).
   - Test limitu N pozycji i braku przekroczenia slotów.

---

## 4. Weryfikacja i Dostarczenie
1. Wszystkie testy (istniejące 60 testów + nowe testy) muszą przechodzić (`pytest`).
2. Uruchomienie Fazy 0: wykonanie bramki i zapis raportu.
3. Commit i push zmian na GitHub (`origin main`).
