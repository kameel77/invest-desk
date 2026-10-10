# Trading Desk

Aplikacja do śledzenia cen i generowania sygnałów kup/sprzedaj na **GPW i
giełdach zagranicznych**, z dashboardem i silnikiem **paper tradingu**
skalowanym pod małe tickety.

> Papier. Realnych zleceń nie składa. Zero ryzyka, pełna pętla danych.

---

## Co to potrafi

| Moduł | Zakres |
|---|---|
| **Dane** | Yahoo Finance (GPW `.WA`, US, ETF-y) + opcjonalnie Alpaca IEX dla real-time US |
| **Wskaźniki** | SMA20/50/200, EMA12/26, RSI14, ATR14, Bollinger, ROC 20/60, ADV, vol. ratio |
| **Silnik sygnałów** | trend + momentum + rewersja, scoring -100…+100, bramki płynności |
| **Paper trading** | zlecenia z prowizją GPW (0,35% + minimum), slippage, stop-loss i take-profit na ATR |
| **Sizing** | risk-per-trade 0,5% kapitału, cap 15% na pozycję, min. ticket 50 zł |
| **Dashboard** | equity curve, przegląd rynku, statystyki sygnałów, pozycje, zlecenia, ranking analizy |
| **Wykres** | modal pełnoekranowy, świece + wolumen, crosshair OHLCV, zakres 3M–2R, 3 zakładki (Wykres / Analiza / Dane spółki) |
| **Fundamentały** | profil, wycena, rentowność, bilans, dywidenda, analitycy (Yahoo quoteSummary, cookie+crumb) |
| **Backtesting** | symulacja bez look-ahead, sweep parametrów, metryki (PF, Sharpe, max DD) |
| **Kontekst rynku** | reżim indeksu, momentum relatywne (RS), VIX — jako bramka wejścia |
| **Walk-forward** | dostrajanie na danych treningowych, ocena na niewidzianych słupkach |
| **API** | FastAPI, pełne REST pod automatyzacje i własne procesy |

---

## Wynik testów strategii (stan na 2026-09-29)

Backtest na 52 instrumentach, 10 lat świec dziennych (141 468 świec).

| Metryka | Obecna strategia | Najlepsza z 19 wariantów |
|---|---|---|
| Konfiguracja | wej≥35, SL2.0, TP3.0, H20 | wej≥35, **SL3.0, TP4.5**, H20 |
| Zwrot (in-sample) | **-11,65%** | **+13,97%** |
| Sharpe | -0,14 | 0,44 |
| Profit factor | 0,97 | 1,06 |
| Transakcje | 1 005 | 844 |

**Wniosek:** za ciasne stopy zabijają tę strategię. Przy SL1,5/TP2,0 zwrot
spada do -81,65%. Wyższy próg wejścia (≥55) również pogarsza wynik. Wagi
komponentów mają znaczenie drugorzędne.

> **Te liczby są z tych samych danych, na których konfiguracja była wybierana.**
> Sweep pokazuje **+13,97%**. Walk-forward na tych samych danych pokazuje
> **-2,02%**. Ta rozbieżność jest całym pointem.

### Walk-forward (5 fałdów, 2 lata treningu + 6 mies. testu, 52 instrumenty)

| Fałd | Wybrana konfiguracja | Trening % | Train Sharpe | **Test %** | **Test Sharpe** | Bazowo % |
|---|---|---|---|---|---|---|
| 1 | SL3.0/TP4.5 | -5,87% | -0,25 | -4,21% | -0,97 | -17,99% |
| 2 | SL3.0/TP4.5 | +14,41% | 0,83 | +2,60% | 0,65 | +2,75% |
| 3 | SL3.0/TP4.5 | -0,85% | 0,00 | +17,16% | 4,71 | +12,03% |
| 4 | SL3.0/TP4.5 | +1,45% | 0,14 | -2,77% | -0,64 | -9,06% |
| 5 | SL3.0/TP4.5 | +24,31% | 1,96 | **-22,86%** | -5,46 | -37,65% |

**Podsumowanie: -2,02% poza próbką** (2/5 fałdów zyskownych, Sharpe -0,34,
535 transakcji) wobec **-9,98% dla konfiguracji domyślnej**.

Trzy rzeczy, które widać dopiero tutaj:

1. **Dostrajanie pomaga, ale nie wystarcza.** Lepsza geometria stopu (SL3.0/TP4.5)
   wygrywała w każdym fałdzie i obcina stratę z -9,98% do -2,02%. Nadal jest to
   strata.
2. **Fałd 5 to kluczowy.** Trening pokazał +24,31% i Sharpe 1,96 — najlepszy
   wynik w tabeli. Test dał -22,86%. To jest overfit w czystej formie: gdyby
   wybrano tę konfigurację po treningu i „wierzono" jej, strata byłaby
   dwucyfrowa. Sweep z poprzedniego dnia pokazałby dokładnie +24% i byłby
   zielony.
3. **Warianty kontekstowe (reżim/RS) nie wygrały ani razu.** We wszystkich
   5 fałdach najlepsza na treningu była ta sama geometria bez bramki
   kontekstowej. Na tej próbie kontekst nie wniósł wartości — dlatego
   `scanner.USE_CONTEXT = False` i nie blokuje sygnałów na żywo.

#### Dowód bezpośredni: filtr kontekstu szkodzi

Ten sam test bez walk-forward, na wszystkich 844 transakcjach z konfiguracją
SL3.0/TP4.5 (`python -m tools.test_context_exit`):

| | Transakcje | PnL | Trajność |
|---|---|---|---|
| Wszystkie | 844 | **+1 399,91 zł** | 50,7% |
| Tylko kontekst przyjazny | 711 | **+3,35 zł** | 49,4% |

Filtr odrzucił 133 wejścia i **usunął 1 396 zł zysku**, a trajność spadła.
Kontekst odrzucał transakcje, które zarabiały — spółki rosły wolniej niż
indeks, ale wciąż rosły, i to wystarczało.

To wyjaśnia, dlaczego żaden wariant kontekstowy nie wygrał na treningu: nie
był marginalnie słabszy, był aktywnie szkodliwy na tej próbie.

**Wniosek:** strategia w obecnej formie nie zarabia poza próbką. Nie jest to
problem dostrajania parametrów, tylko brak przewagi w samym sygnale
(trend+momentum na średnim horyzoncie). Dalsze ulepszanie wymaga zmiany
hipotezy, nie optymalizacji.

### Test innych hipotez (`python -m tools.run_hypotheses`)

Przetestowane cztery rodziny, każda przez ten sam walk-forward. Miarą
porównawczą jest **expectancy na transakcję** (zwrot skumulowany zależy od
liczby transakcji i myli).

| Rodzina | OOS transakcje | Expectancy | Zyskowna fałdy | Sharpe |
|---|---|---|---|---|
| **Rewersja do średniej** | 590 | **+0,467%** | **3/4** | **1,13** |
| Surowce/FX (driver) | 1 776 | +0,116% | 1/4 | 0,23 |
| Średnie kroczące (per-symbol) | 265 | -0,01% | 1/2 | -0,22 |
| Momentum krótkie | 3 900 | -0,426% | 0/4 | -1,77 |

### Weryfikacja hipotezy rewersji po parytecie z produkcją (stan na 2026-10-09)

Po wdrożeniu pełnego parytetu symulatora z regułami produkcyjnymi (ATR z True Range,
SL 2.5 ATR, TP 3.0 ATR, filtr rozciągnięcia -1.0 ATR, priorytet stopu przed celem,
obsługa luk na otwarciu, multiplikatywne koszty prowizji i poślizgu ~0,9% round-trip)
przeprowadzono rzetelny walk-forward (8 fałdów, 2 lata treningu + 5 mies. testu, 52 instrumenty):

| Badanie / Wariant | Transakcje OOS | Expectancy OOS (netto) | Zyskowne fałdy | GPW (netto) | US (netto) |
|---|---|---|---|---|---|
| **Etap A: Selekcja w siatce (12 wariantów)** | 449 | **-0,308%** | 2 / 8 | — | — |
| **Baza (lookback=20, z=2.0, h=10, SL2.5, TP3.0)** | 741 | **-0,103%** | 3 / 8 | **-0,624%** | **+0,727%** |
| Sąsiad: lookback=15 | 846 | -0,237% | 3 / 8 | -0,586% | +0,266% |
| Sąsiad: lookback=30 | 579 | **+0,379%** | 6 / 8 | **-0,244%** | **+1,353%** |
| Sąsiad: z_entry=1.75 | 967 | -0,174% | 3 / 8 | -0,679% | +0,560% |
| Sąsiad: hold=5 | 785 | -0,412% | 2 / 8 | -0,812% | +0,230% |
| Sąsiad: hold=15 | 726 | -0,016% | 4 / 8 | -0,609% | +0,919% |
| Odporność: koszty 1,5× (prowizja 0,52%) | 741 | -0,551% | 2 / 8 | -1,071% | +0,274% |

**Wnioski architektoniczne:**
1. **GPW jest ujemne we wszystkich 12 wariantach (-0,24% do -0,81%):**
   Koszty transakcyjne na GPW (0,35% + poślizg) przy niskiej zmienności i częstych
   długotrwałych trendach spadkowych zjadają zysk z krótkiej rewersji.
2. **Rynek US jest dodatni we wszystkich wariantach (+0,27% do +1,35%):**
   Wyższa płynność i dynamika odbić technicznych na US tworzy autentyczną przewagę rynkową.
3. **Decyzje dotyczące paper tradingu na żywo:**
   - `TREND_ENABLED = False`: brak nowych wejść trendu (brak potwierdzonego edge'u OOS).
   - `REVERSION_GPW_ENABLED = False`: sygnały rewersji na GPW są wyłącznie informacyjne (dla tradera manualnego);
     automatyczne zlecenia paper tradingu są dopuszczone wyłącznie dla rynku US.

### Wyniki Bramki Walidacyjnej Fazy 0 dla US Reversion (2026-10-09)

Zgodnie z wymogami ilościowymi (konsultacja Claude Opus 5.5), przed wdrożeniem silnika portfelowego
przeprowadzono badanie wrażliwości strategii na profile kosztowe brokerów oraz test istotności statystycznej
z **block bootstrapem** (bloki tygodniowe, 2000 iteracji) na 352 transakcjach OOS:

```bash
python3 tools/run_hypotheses.py --gate --profile IBKR_TIERED_USD
```

| Profil Brokera | Transakcje | Expectancy OOS | 90% CI (Block Bootstrap) | Fałdy (N≥25 & >0) | Werdykt Bramki |
|---|---|---|---|---|---|
| **IBKR_TIERED_USD** (konto USD) | 352 | **+1,356%** | **[+0,603%, +2,115%]** | **7 / 8** | **GO (Zaliczona)** |
| **XTB_PLN** (narzut FX 1.0% round-trip) | 352 | +0,355% | [-0,391%, +1,106%] | 4 / 8 | FAILED (ujemne CI) |
| **CONSERVATIVE_US** (stress test) | 352 | +0,054% | [-0,690%, +0,803%] | 4 / 8 | PASSED (exp ≥ 0) |

**Kluczowe odkrycie Fazy 0:**
Narzut przewalutowania (0,5% w każdą stronę = 1,0% round-trip) na koncie w PLN całkowicie degraduje
statystyczną jakość edge'u (dolna granica CI spada poniżej zera). **Krótkoterminowa rewersja na US musi
być handlowana na rachunku w USD (IBKR Tiered lub subkonto walutowe USD w XTB)**, gdzie wymiana waluty
następuje jednorazowo przy wpłacie kapitału, a nie przy każdej transakcji.

### Wyniki Symulacji Portfelowej Fazy 1 (`tools/run_portfolio_sim.py`)

Po przejściu bramki Fazy 0 zbudowano symulator portfela (`app/portfolio_sim.py`) z pętlą zdarzeń bar-by-bar,
zarządzaniem wolnymi slotami i deterministycznym kolejkowaniem (najgłębszy z-score pierwszy, tie-breaker po symbolu):

```bash
python3 tools/run_portfolio_sim.py --slots 8 --profile IBKR_TIERED_USD
```

| Parametr / Wynik | 5 slotów (IBKR Tiered) | 8 slotów (IBKR Tiered) | 8 slotów (XTB PLN z FX) | Benchmark SPY B&H |
|---|---|---|---|---|
| **Łączny zwrot portfela** | +21,31% | **+35,07%** | -20,73% | +78,04% |
| **CAGR (roczna stopa zwrotu)** | +3,96% | **+6,23%** | -4,56% | +12,29% |
| **Max Drawdown (MDD)** | -26,24% | **-18,92%** | -28,11% | -25,36% |
| **Sharpe Ratio (rf=0%)** | 0,332 | **0,560** | -0,330 | — |
| **Sortino Ratio** | 0,420 | **0,708** | -0,408 | — |
| **Liczba transakcji** | 401 | 511 | 508 | — |
| **Win Rate** | 55,1% | **57,1%** | 53,0% | — |
| **Odrzucone sygnały (brak slotu)** | 416 | **196** | 194 | — |

**Wnioski portfelowe:**
1. **Dywersyfikacja na 8 slotów (12,5% kapitału)** znacząco redukuje Max Drawdown z -26,24% do **-18,92%**
   (znacznie poniżej rynku SPY -25,36%) i podnosi Sharpe powyżej 0,50 (0,560), redukując liczbę
   odrzuconych sygnałów podczas rynkowych wyprzedaży o ponad 50%.
2. **Konto w PLN z narzutem FX przynosi stratę portfela -20,73%**: 500 transakcji obciążonych 1% FX round-trip
   niszczy krzywą kapitału. Rachunek musi być prowadzony w USD.

*Uwaga: tabele Fazy 0 i Fazy 1 powyżej to wyniki historyczne z 2026-10-09 (baza 5-letnia), sprzed poprawek silnika portfelowego (liczenie dni trzymania po barach symbolu, odroczone wejścia, warmup sygnału) i nie zostały przeliczone. Aktualne liczby portfela są w sekcji Bramka v2.*

### Bramka v2

Po audycie Fazy 0 i 1 przeprowadzono pełną walidację długoterminową (19 fałdów rocznych, 2008–2026) na uniwersum 22 symboli US (15 akcji + 7 ETF-ów) pod kątem odporności strategii na różne reżimy rynkowe, kryzysy (2008, 2020, 2022) oraz model kosztowy brokera detalicznego **XTB_USD** (0% prowizji, 0,05% poślizgu, brak narzutu FX, akcje ułamkowe, kapitał startowy \$1 350, 8 slotów). Dane: czysta baza 20-letnia (każdy z symboli bez odrzuconych barów, SPY: 5 031 barów, 2006-10-09 do 2026-10-08):

```bash
python3 tools/run_hypotheses.py --gate-v2          # domyślny profil: XTB_USD
python3 tools/run_portfolio_sim.py --slots 8 --profile XTB_USD
```

Raporty: `reports/gate_v2_20261010_102637_XTB_USD_8slots.json` i `reports/portfolio_us_sim_20261010_102643_XTB_USD_8slots.json`.

**Survivorship bias.** Uniwersum to dzisiejsza watchlista 22 instrumentów (m.in. NVDA, TSLA, META, AVGO, LLY), czyli spółki, które przetrwały i urosły; spółki wycofane z obrotu lub te, które nie dotrwały do dziś, w próbie nie występują. Wynik na akcjach jest przez to zawyżony. ETF-y (SPY, QQQ, IWM, EEM, EFA, EWZ, VGK) nie mają tego problemu w takim stopniu i są bardziej wiarygodną częścią próby.

#### 1. Podsumowanie 6 Kryteriów Bramki v2

| # | Kryterium Bramki v2 | Wartość zmierzona | Wymóg | Werdykt |
|---|---|---|---|---|
| **1** | **Pełny okres (2008–2026): 90% CI dolna (block bootstrap)** | **+0,046%** | > +0,10% | **FAILED** |
| **2** | **Holdout (< 2021-10-01): Expectancy / 90% CI dolna** | **+0,277% / -0,192%** | exp > 0, CI_min > -0,25% | **PASSED** |
| **3** | **Zyskowne fałdy roczne (N ≥ 25 transakcji)** | **14 / 19 (73,7%)** | ≥ 60,0% | **PASSED** |
| **4** | **Stress test kosztowy (`CONSERVATIVE_US`)** | **-1,823%** | ≥ 0,00% | **FAILED** |
| **5** | **Symulacja portfela: najgorszy pełny rok kalendarzowy** | **-35,60% (rok 2008)** | ≥ -15,0% | **FAILED** |
| **6** | **Test uzgodnienia silnika (`simulate()` ↔ `PortfolioSim`)** | **Zgodność 1:1 (błąd < 1e-3 pp; pytest: 3 seedy na jednym symbolu + 3 seedy na dwóch symbolach z różnymi kalendarzami)** | Zielony test jednostkowy | **PASSED** |
| **—** | **WERDYKT KOŃCOWY BRAMKI v2** | **3 z 6 kryteriów spełnione** | Wszystkie 6 | **NO-GO** |

---

#### 2. Tabela Rocznych Wyników (2008–2026)

Poniższa tabela przedstawia szczegółowe rozbicie wyników z podziałem na ETF-y i pojedyncze akcje oraz porównanie zachowania
całego portfela 8-slotowego (\$1 350 kapitału startowego, akcje ułamkowe) z rynkowym benchmarkiem **SPY Buy & Hold**.
Reguła roku startowego: fałdy roczne obejmują każdy rok od pierwszego, w którym co najmniej jeden symbol ma ≥ 60 barów przed 1 stycznia, a transakcje danego symbolu liczą się dopiero od roku, w którym ten symbol sam spełnia ten warunek.

| Rok | N transakcji | Win Rate | Expectancy (XTB_USD) | ETF-y (N / Exp.) | Akcje (N / Exp.) | Portfel PnL (\$) | Portfel Zwrot % | SPY B&H % |
|---|---|---|---|---|---|---|---|---|
| **2008** | 149 | 30,2% | -2,344% | 65 / -2,668% | 84 / -2,093% | -\$492,21 | **-35,60%** | **-37,74%** |
| **2009** | 74 | 64,9% | +1,819% | 25 / +0,010% | 49 / +2,742% | +\$164,89 | +18,52% | +19,88% |
| **2010** | 87 | 40,2% | -1,013% | 31 / -2,786% | 56 / -0,031% | +\$11,08 | +1,05% | +10,96% |
| **2011** | 132 | 59,1% | +0,951% | 49 / +0,157% | 83 / +1,420% | +\$131,44 | +12,33% | -1,22% |
| **2012** | 99 | 48,5% | -0,364% | 36 / -0,155% | 63 / -0,483% | -\$87,29 | -7,29% | +11,69% |
| **2013** | 97 | 68,0% | +0,999% | 41 / +0,655% | 56 / +1,252% | +\$135,79 | +12,23% | +26,45% |
| **2014** | 133 | 56,4% | +0,771% | 55 / +0,206% | 78 / +1,170% | +\$135,55 | +10,88% | +12,37% |
| **2015** | 133 | 65,4% | +1,088% | 61 / +0,262% | 72 / +1,788% | +\$197,98 | +14,33% | -0,76% |
| **2016** | 130 | 56,2% | +0,378% | 42 / +1,308% | 88 / -0,066% | +\$41,56 | +2,63% | +11,20% |
| **2017** | 74 | 78,4% | +0,964% | 28 / +1,003% | 46 / +0,941% | +\$137,54 | +8,48% | +18,48% |
| **2018** | 148 | 57,4% | +1,147% | 58 / -0,010% | 90 / +1,893% | +\$106,22 | +6,04% | -7,01% |
| **2019** | 90 | 53,3% | +0,354% | 35 / +0,313% | 55 / +0,381% | +\$10,81 | +0,58% | +28,65% |
| **2020** | 119 | 50,4% | -0,330% | 41 / -2,949% | 78 / +1,047% | -\$21,88 | -1,17% | +15,09% |
| **2021** | 115 | 59,1% | +0,567% | 48 / +0,518% | 67 / +0,602% | +\$118,69 | +6,40% | +28,79% |
| **2022** | 162 | 48,1% | -0,304% | 58 / +0,342% | 104 / -0,664% | -\$249,66 | **-12,66%** | **-19,95%** |
| **2023** | 85 | 67,1% | +1,570% | 32 / +0,637% | 53 / +2,134% | +\$207,11 | +12,02% | +24,81% |
| **2024** | 100 | 63,0% | +0,908% | 38 / +0,176% | 62 / +1,356% | +\$236,51 | +12,25% | +24,00% |
| **2025** | 105 | 59,0% | +1,662% | 32 / +1,252% | 73 / +1,841% | +\$301,08 | +13,90% | +16,64% |
| **2026** | 97 | 57,7% | +0,940% | 30 / +0,136% | 67 / +1,300% | +\$187,42 | +7,59% | +13,29% |
| **Suma / Śr.** | **2 129** | **55,9%** (portfel: **55,5%**) | **+0,432%** | **805 / -0,143%** | **1 324 / +0,782%** | **+\$1 272,63** | **+92,05%** | **+392,78%** |

*Uwaga: Statystyki per transakcja (N, Win Rate, Expectancy) pochodzą z `simulate()`, natomiast PnL (\$) i Portfel % z pełnej symulacji portfela `PortfolioSim`. Wiersz sumaryczny: PnL i zwroty portfela liczone od początku 2008 (kapitał \$1 382,51) do \$2 655,14; SPY B&H to złożenie rocznych zwrotów z tabeli (rok 2026 niepełny, do 2026-10-08). Win Rate w sumie to średnia ważona liczbą transakcji.*

*Portfel w całym okresie 2006-10-09 – 2026-10-08: +96,68% (SPY B&H +472,90%), CAGR +3,45% (SPY +9,14%), Max Drawdown -43,83% (SPY -56,47%), 1 882 transakcje, Win Rate 55,5%, Profit Factor 1,17.*

---

#### 3. Podział: ETF vs Pojedyncze Akcje

1. **Przewaga akcji nad ETF-ami w fazach normalnych:**
   - Na przestrzeni 19 lat pojedyncze akcje osiągnęły średnie expectancy **+0,782%** wobec **-0,143%** dla ETF-ów.
   - Akcje wykazują znacznie większą dynamikę odbić po skrajnych wyprzedaniach (z-score ≤ -2.0). Wynik na akcjach jest jednak zawyżony przez survivorship bias (patrz wyżej), więc to ETF-y są tu wiarygodniejszym punktem odniesienia.
2. **Zachowanie ETF-ów w czasie załamań rynkowych (2008, 2010, 2020):**
   - Podczas krachów rynkowych ETF-y zaliczały dotkliwe serie strat (np. 2008: expectancy **-2,668%**, 2010: **-2,786%**, 2020: **-2,949%**).
   - Mechanizm rewersji na ETF-ach w silnym trendzie spadkowym prowadzi do łapania "spadających noży" — ETF-y indeksowe nie odbijają natychmiastowo, lecz osuwają się przez wiele sesji z rzędu, wybijając kolejne stop-lossy.

---

#### 4. Wnioski, Reżimy Rynkowe i Uzasadnienie NO-GO

Strategia uzyskała werdykt **NO-GO** z trzech kluczowych powodów:

1. **Brak ochrony przed głęboką bessą (Kryterium 5: strata -35,60% w 2008 r.):**
   - Dopuszczalny próg rocznego drawdownu portfela wynosił -15,0%. W 2008 roku portfel stracił aż -35,60%, a w 2022 roku -12,66%.
   - Łączny Max Drawdown w ujęciu długoterminowym wyniósł **-43,83%**.
   - **Przyczyna:** Strategia nie posiada obecnie aktywnego filtru reżimu rynkowego (np. 200-dniowa średnia na indeksie SPY lub filtr zmienności VIX), przez co otwiera pozycje długie nawet w trakcie globalnych załamań płynnościowych.
2. **Niewystarczający margines statystyczny na pełnej próbie (Kryterium 1: CI dolna +0,046% vs próg +0,10%):**
   - Chociaż wartość oczekiwana netto wynosi +0,432%, miesięczny block bootstrap (uwzględniający autokorelację) wykazuje, że dolny 90% przedział ufności schodzi do +0,046%, co nie daje wystarczającego bufora bezpieczeństwa.
3. **Ekstremalna wrażliwość na koszty (Kryterium 4: stress test CONSERVATIVE_US = -1,823%):**
   - Strategia generuje edge wyłącznie przy zerowych prowizjach (`XTB_USD`).
   - Wprowadzenie realnych opłat giełdowych lub minimalnych biletów brokerskich (np. IBKR Tiered \$0,35 / \$1,00 na pozycję przy kapitale \$168/slot) zamienia wynik w wyraźną stratę.

---

#### 5. Rekomendacje dla Fazy Rozwoju

Przed dopuszczeniem strategii do realnego tradingu wymagane są następujące usprawnienia:
- **Filtr Reżimu Indeksu (Trend/Vol Filter):** Blokada otwierania nowych transz długich, gdy SPY znajduje się poniżej SMA200 lub gdy 10-dniowy wskaźnik zmienności przekracza poziomy paniki.
- **Dynamiczne zarządzanie slotami / Circuit Breakers:** Wstrzymanie alokacji kapitału przy skumulowanym drawdownie bieżącego miesiąca przekraczającym 5%.
- **Optymalizacja wyjść:** Zastąpienie sztywnego stopu czasowego (10 barów) dynamicznym powrotem do średniej (np. wyjście na SMA20 lub przecięciu z-score = 0).

---

### Bramka v2: Hipoteza Filtra Bessy (SPY > SMA200) — KAM-91

Zgodnie z rekomendacjami z audytu Fazy 1 przetestowano jedną, z góry zadeklarowaną hipotezę:
**Strategia US mean-reversion otwiera nowe pozycje wyłącznie wtedy, gdy indeks SPY znajduje się powyżej swojej 200-sesyjnej średniej kroczącej (`Close(t) > SMA200(Close)(t)`).**

Filtr jest w pełni wolny od efektu lookahead (decyzja zapada na zamknięciu słupka sygnału \(t\), wejście następuje na otwarciu \(t+1\); pierwsze 199 sesji traktowane jest jako faza rozgrzewki = brak wejść). Filtr blokuje wyłącznie **nowe wejścia** — otwarte wcześniej pozycje są zamykane normalnymi regułami wyjścia.

```bash
python3 tools/run_hypotheses.py --gate-v2 --profile XTB_USD --regime-filter spy200
```

#### 1. Zestawienie 6 Kryteriów Bramki v2 (Bez Filtra vs Z Filtrem SPY > SMA200)

| # | Kryterium Bramki v2 | Bez Filtra (KAM-90) | Z Filtrem `spy200` (KAM-91) | Wymóg Formalny | Wpływ Filtra |
|---|---|---|---|---|:---:|
| **1** | **Pełny okres: 90% CI dolna (block bootstrap)** | +0,092% (FAILED) | **+0,181% (PASSED)** | > +0,10% | ✅ **ZALICZONE** |
| **2** | **Holdout (< 2021-10-01): Exp / 90% CI dolna** | +0,335% / -0,127% (PASSED) | **+0,535% / +0,094% (PASSED)** | exp > 0, CI_min > -0,25% | ✅ **ZNACZNY WZROST** |
| **3** | **Zyskowne fałdy roczne (N ≥ 25)** | 14 / 19 (73,7%) (PASSED) | **15 / 19 (78,9%) (PASSED)** | ≥ 60,0% | ✅ **WZROST (15/19)** |
| **4** | **Stress test kosztowy (`CONSERVATIVE_US`)** | -1,793% (FAILED) | **-1,708% (FAILED)** | ≥ 0,00% | ❌ **FAILED** |
| **5** | **Portfel: najgorszy pełny rok kalendarzowy** | -33,92% [rok 2008] (FAILED) | **-9,85% [rok 2012] (PASSED)** | ≥ -15,0% | ✅ **ZALICZONE (2008: 0,00%)** |
| **6** | **Test uzgodnienia silnika (`simulate` ↔ `PortfolioSim`)** | Zgodność 1:1 na 3 seedach (PASSED) | **Zgodność 1:1 na 3 seedach (PASSED)** | Zielony test jednostkowy | ✅ **PASSED** |
| **—** | **WERDYKT BRAMKI V2** | **NO-GO (3 z 6 zaliczone)** | **NO-GO (5 z 6 zaliczonych)** | Wszystkie 6 = PASSED | **NO-GO** |

---

#### 2. Wpływ Filtra na Statystyki i Ryzyko Portfela

- **Aktywność reżimu rynkowego:** Filtr był włączony (`SPY > SMA200`) przez **76,7%** sesji w latach 2008–2026.
- **Usunięte transakcje:** Filtr wyeliminował **772** transakcje o średnim expectancy **+0,376%**. 
  - **Ważny wniosek:** usunięte transakcje miały *dodatnie* expectancy netto w normalnych warunkach, co oznacza, że filtr odcina część potencjalnych zysków. 
  - **Kluczowa korzyść:** eliminacja ta odbywa się kosztem odcięcia katastrofalnych wejść w okresach załamań rynkowych (np. w 2008 roku usunięto 152 transakcje o skrajnie ujemnym expectancy -2,28%), co radykalnie poprawiło profil ryzyka portfela.
- **Kluczowe metryki portfela (\$1 350, 8 slotów, `XTB_USD`, akcje ułamkowe):**
  - **Max Drawdown (MDD):** redukcja z **-42,53%** do zaledwie **-15,92%** (wobec rynkowego SPY MDD -56,47%).
  - **Win Rate:** wzrost z **55,3%** do **57,4%**.
  - **Profit Factor:** wzrost z **1,16** do **1,26**.
  - **Sharpe Ratio (rf=0%):** wzrost z **0,295** do **0,447**.

---

#### 3. Roczne Wyniki Portfela Rok po Roku (2008–2026: Bez Filtra vs Z Filtrem SPY > SMA200 vs SPY B&H)

| Rok | Portfel Bez Filtra | Portfel Z Filtrem `spy200` | SPY B&H | Wpływ filtra / Komentarz |
|---|---|---|---|---|
| **2008** | **-33,92%** (152 trans.) | **0,00% (0 trans.)** | **-34,31%** | **Perfekcyjna ochrona:** portfel w 100% w gotówce przez cały rok krachu. |
| **2009** | +24,03% | +16,47% | +34,54% | Późniejsze wznowienie wejść po powrocie SPY nad SMA200. |
| **2010** | +2,56% | -9,27% | +17,10% | Krótkotrwałe wyjścia/wejścia wokół SMA200 (Flash Crash). |
| **2011** | +11,22% | +5,34% | -2,47% | Ograniczenie ekspozycji w czasie obniżenia ratingu USA. |
| **2012** | -6,78% | **-9,85%** | +8,45% | Najgorszy pełny rok portfela (zgodny z wymogiem ≤ -15%). |
| **2013** | +10,93% | +10,93% | +23,37% | Pełna zgodność w silnym rynku byka. |
| **2014** | +8,74% | +2,53% | +15,36% | Kilkukrotne naruszenia SMA200 odcięły część wejść. |
| **2015** | +6,68% | +10,69% | +2,22% | Poprawa wyniku dzięki ominięciu korekty sierpniowej. |
| **2016** | +3,58% | +6,51% | +15,39% | Odcięcie spadków z początku roku. |
| **2017** | +9,42% | +9,42% | +17,29% | Pełna zgodność w stabilnym trendzie wzrostowym. |
| **2018** | +5,79% | +3,87% | -11,34% | Ominięcie korekty z IV kwartału 2018 r. |
| **2019** | -2,32% | -6,23% | +19,24% | Płaski rok po grudniowym dołku 2018. |
| **2020** | -0,69% (119 trans.) | **+4,89%** (69 trans.) | +16,21% | **Ochrona przed krachem Covid:** odcięcie paniki z marca. |
| **2021** | +5,96% | +5,96% | +28,34% | Identyczny wynik w stabilnym rynku byka. |
| **2022** | **-13,32%** (162 trans.) | **-7,58%** (21 trans.) | **-15,00%** | **Redukcja straty niemal o połowę** w czasie bessy inflacyjnej. |
| **2023** | +12,78% | +5,48% | +16,93% | Wejścia odblokowane po powrocie SPY nad SMA200. |
| **2024** | +9,82% | +9,82% | +21,37% | Zgodność w stabilnym trendzie. |
| **2025** | +14,13% | +7,36% | +13,31% | Spokojny wzrost z mniejszą liczbą transz. |
| **2026** | +10,47% | +6,24% | +11,84% | Stabilny dodatni wynik w bieżącym okresie. |

---

#### 4. Badanie Wrażliwości Długości Średniej (Informacyjnie: SMA150, SMA200, SMA250)

Aby zweryfikować, czy skuteczność filtra nie wynika z przypadkowego dopasowania do okna 200 sesji, przeprowadzono identyczną procedurę dla sąsiadujących parametrów **SMA150** oraz **SMA250**:

| Parametr filtra | Aktywność % | Transakcje | Expectancy (netto) | 90% CI dolna (mies.) | Holdout CI dolna | Najgorszy rok portfela | Zwrot 2008 r. | Max Drawdown | Kryteria zaliczone |
|---|---|---|---|---|---|---|---|---|:---:|
| **`spy150`** | 75,9% | 1 284 | +0,556% | +0,194% | +0,087% | -9,95% (2012) | -0,56% (1 trans.) | -16,40% | **5 / 6** |
| **`spy200` (baza)** | 76,7% | 1 394 | +0,560% | +0,181% | +0,094% | -9,85% (2012) | **0,00% (0 trans.)** | **-15,92%** | **5 / 6** |
| **`spy250`** | 77,1% | 1 439 | +0,528% | +0,171% | +0,050% | -9,14% (2012) | **0,00% (0 trans.)** | -16,85% | **5 / 6** |

**Wniosek z badania wrażliwości:**
Wyniki w całym paśmie 150–250 sesji są niemal identyczne — w każdym wariancie zaliczone jest dokładnie 5 z 6 kryteriów, a obsunięcie portfela w 2008 roku zostaje zredukowane praktycznie do zera. Potwierdza to strukturalną odporność hipotezy na wybór parametru.

---

#### 5. Uzasadnienie Werdyktu NO-GO

Mimo że filtr `SPY > SMA200` rozwiązał fundamentalne problemy strategii (zaliczył kryterium dolnego CI +0,181% oraz zredukował stratę 2008 roku z -33,92% do 0,00%), **ostateczny werdykt formalny to NO-GO**:
- Kryterium 4 (stress test `CONSERVATIVE_US`) przyniosło wynik **-1,708%** (wobec wymaganego ≥ 0,0%).
- Hipoteza potwierdza, że strategia rewersji wymaga bezwzględnie rachunku z **zerową stawką prowizji (`XTB_USD`)** i nie jest odporna na klasyczne prowizje biletowe przy małym nominale transzy (\$168).
- Zgodnie z założeniami metodologicznymi Fazy 2: brak zaliczenia 1 z 6 kryteriów uniemożliwia przejście do tradingu na realnym kapitale, lecz stanowi solidną bazę pod paper trading.

## Uruchomienie

```bash
cd ~/Documents/Coding/github/trading-desk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run.sh                      # http://127.0.0.1:8770
```

Opcjonalnie, dla prawdziwego real-time na USA (darmowy tier IEX):

```bash
export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
```

## Konfiguracja (env)

| Zmienna | Domyślnie | Znaczenie |
|---|---|---|
| `TD_PORT` | `8770` | Port serwera |
| `TD_STARTING_CASH` | `10000` | Kapitał startowy (PLN / USD) |
| `TD_COMMISSION_PCT` | `0.0035` | Prowizja % (GPW retail) |
| `TD_COMMISSION_MIN` | `1.00` | Prowizja minimalna |
| `TD_SLIPPAGE_PCT` | `0.001` | Slippage bazowy |
| `TD_MIN_TICKET` | `50` | Minimalny nominal zlecenia |
| `TD_SCAN_INTERVAL` | `300` | Interwał automatycznego skanu (s) |
| `TD_TREND_ENABLED` | `0` | Zezwalaj na nowe wejścia trendu (1/0) |
| `TD_REVERSION_HOLD_DAYS` | `10` | Limit świec trzymania transzy rewersji |
| `TD_REVERSION_GPW_ENABLED` | `0` | Zezwalaj na transakcje rewersji na GPW (1/0) |
| `TD_BROKER_PROFILE_US` | `IBKR_TIERED_USD` | Profil brokera dla rynku US |
| `TD_BROKER_PROFILE_GPW` | `GPW_RETAIL` | Profil brokera dla GPW |
| `TD_US_MAX_SLOTS` | `5` | Maksymalna liczba równoczesnych pozycji US |
| `TD_US_SLOT_PCT` | `0.20` | Docelowa alokacja kapitału na slot US |
| `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` | — | Włącza real-time US |


## Strategia (domyślna)

`trend-momentum-long`, horyzot dni–tygodnie:

- **Trend (40%)** — pozycja ceny vs SMA50/SMA200 + nachylenie SMA50 znormalizowane ATR
- **Momentum (35%)** — ROC 20/60 dni, RSI z poprawką na wykupienie/wyprzedanie, slope SMA50
- **Rewersja (25%)** — pozycja %B Bollingera i z-score; rozciąga wynik, nie wywołuje wejścia
- **Bramka płynności** — 20-dniowy ADV ≥ 1 mln USD (US) / 300 tys. PLN (GPW)

Próg wejścia: score ≥ **+35**. Próg wyjścia: score ≤ **-25** (tniej: szybciej
tnijemy straty niż realizujemy zyski).

## API

```
GET  /api/health               stan usługi, źródła danych
GET  /api/account              equity, gotówka, pozycje
POST /api/account/reset        wyzeruj konto papierowe
GET  /api/watchlist            lista instrumentów
POST /api/watchlist            dodaj instrument
GET  /api/quotes?refresh=1     kursy (refresh wymusza odświeżenie)
GET  /api/candles/{symbol}     świece OHLCV
GET  /api/analysis/{symbol}    pełna analiza jednego instrumentu
GET  /api/scan?execute=0       uruchom skan (execute=1 = wykonaj sygnały)
GET  /api/signals              historia sygnałów
GET  /api/signals/stats        statystyki: rozkład, rynki, uzasadnienia
GET  /api/market/stats         snapshot uniwersum: score, najlepsze/najgorsze, ruchy
GET  /api/fundamentals/{sym}   profil, wycena, bilans, dywidenda, analitycy
GET  /api/fundamentals         fundamentały dla całej listy
GET  /api/backtest             backtest całej listy (?buy= próg wejścia)
GET  /api/backtest/sweep       pełny grid parametrów (wolne, minuty)
GET  /api/backtest/{symbol}    backtest jednego instrumentu
GET  /api/positions            pozycje + equity
GET  /api/orders               zlecenia
POST /api/orders               {"symbol":"CDR.WA","side":"BUY"} (qty opcjonalne)
GET  /api/equity               krzywa kapitału
```

## Testy sygnałów

Sweep parametrów działa automatycznie:

```bash
./run_daily.sh      # odświeża dane, przepuszcza grid, zapisuje raport
```

```bash
# crontab — sesje GPW i US to tydzień, 30 min po zamknięciu
30 18 * * 1-5 ~/Documents/Coding/github/trading-desk/run_daily.sh
```

Wynik ląduje w `reports/latest.md`, `reports/latest.json` oraz w tabeli
`backtest_runs` (pełna historia do porównania dzień do dnia). Grid obejmuje
próg wejścia, geometrię stop/cel, horyzot trzymania, wagę rewersji i cztery
warianty wag komponentów.

Metoda: brak look-ahead (sygnał na barze *i* liczy wskaźniki tylko z barów
0..*i*), wejście po otwarciu następnego dnia, stop/cel realizowane na
dotkniętym poziomie a nie na zamknięciu, te same prowizje i slippage co
paper trading.

### Walk-forward — jedyna uczciwa miara

Sweep wybiera konfigurację na tych samych słupkach, na których ją potem
ocenia. Przy 19 wariantach i ~1200 słupkach część z nich wygra przypadkowo.
Walk-forward usuwa tę cykliczność:

```
 dla każdego fałdu:
   1. TRENING  na słupkach [start, cięcie)      -> wybór najlepszej konfiguracji
   2. TEST     na słupkach [cięcie, koniec)     -> ocena TEJ konfiguracji
   3. raportujemy wyniki z testu
```

Fałdy idą wyłącznie do przodu w czasie (anchored, bez tasowania). Wynik
testowy jest jedyną liczbą, która mówi, czy strategia działa.

```bash
curl "http://127.0.0.1:8770/api/walkforward?folds=5"
```

### Kontekst rynku (bramka wejścia)

Trzy sygnały spoza samej spółki, wszystkie liczone wyłącznie z przeszłości:

| Sygnał | Znaczenie | Próg |
|---|---|---|
| `regime` | indeks (GPW.WA / ^SPX) powyżej własnego SMA200 | wymagany `up` |
| `rs` | ROC 60 dni spółki minus ROC 60 dni indeksu | `RS > 0` lub `RS > 5` |
| `vix` | poziom zmienności rynkowej | > 22 → pozycja 60%, > 30 → brak wejścia |

Benchmark jest wyrównywany metodą as-of (wartość z **przeszłości** wyłącznie),
więc słupki kontekstu nie mogą wyciekać z przyszłości do cech spółki.

## Struktura

```
  hypotheses.py   4 rodziny strategii (MA, driver, rewersja, momentum krótkie)
                 + selekcja płynnych ruchów, testy przez walk-forward
app/
  config.py      ustawienia + watchlist
  db.py          SQLite (WAL): candles, quotes, signals, orders, positions, equity
  market.py      Yahoo Finance + Alpaca, cache, batching z limitem konkurencji
  fundamentals.py quoteSummary (cookie+crumb): profil, wycena, bilans, analitycy
  signals.py     wskaźniki, scoring, sizing
  execution.py   paper trading: prowizje, slippage, stop-y, equity
  scanner.py     pełen przebieg: dane → analiza → sygnały → wykonanie → snapshot
  backtest.py    symulacja bez look-ahead + sweep parametrów
  context.py     reżim indeksu, momentum relatywne, VIX (bramka wejścia)
  walkforward.py dostrajanie na treningu, ocena na niewidzianych słupkach
  analytics.py   statystyki przekrojowe dla pulpitu i widoku sygnałów
  main.py        FastAPI + cykl skanowania w tle
  static/        dashboard (vanilla JS, bez build stepu): app, fundamentals,
                 analytics, backtest
tools/           diagnostyka i zadania automatyczne (patrz niżej)
reports/         raporty dziennych testów (latest.md, latest.json)
data/trading.db  baza (tworzona automatycznie)
```

## Narzędzia

```bash
.venv/bin/python -m tools.check_all             # health check wszystkich endpointów
.venv/bin/python -m tools.check_dashboard       # spójność id HTML ↔ JS
.venv/bin/python -m tools.check_stale           # audyt martwych notowań
.venv/bin/python -m tools.check_chart_render CDR.WA   # test renderowania wykresu
.venv/bin/python -m tools.resolve_gpw "KGHM"    # nazwa firmy → ticker .WA
.venv/bin/python -m tools.daily_backtest        # dzienny sweep parametrów
.venv/bin/python -m tools.test_context_exit     # test filtra kontekstu
.venv/bin/python -m tools.probe_modules CDR.WA  # jakie pola Yahoo faktycznie zwraca
```

## Dobre praktyki przy rozbudowie

**Real-time na GPW nie jest darmowy.** Yahoo daje dane opóźnione/EOD. Prawdziwy
real-time na GPW wymaga licencji (GPW/Izba, Info Scarborough) albo konta
maklerskiego — najtaniej wychodzi **IBKR**: subskrypcja daje real-time na
GPW i US, a API (`ib_insync`) wystawia dane i zlecenia w jednym miejscu.
Aplikacja jest przygotowana na to: `execution.py` to jedyny moduł, który
zawiera logikę wypełnienia — podmiana na brokera to zamiana `buy`/`sell`.

**Dane historyczne do backtestów** — Yahoo wystarczy do 1–2 lat dziennych.
Do dłuższych okresów i do audytu strategii: Stooq (GPW, CSV za darmo) albo
płatne API.

## Znane ograniczenia

- **Transze zamiast pozycji.** Konto trzyma jedną transzę na wejście, nie
  jedną pozycję na walor. Ponowne wejście w ten sam walor otwiera drugą
  transzę z własnym wejściem, stopem i celem — nie uśrednia się z pierwszą.
  Uśrednianie ukrywa atrybucję: transza +11% przeciw dwóm stratom czyta się
  jako pozycja „-0,10%, bez przewagi". To też kształt wymagany przez polski PIT
  (zysk liczy się dla zbytych transz). Każda transza ma własne poziomy liczone
  **od jej wejścia** oraz stop czasowy (10 świec rewersja / 20 trend).
  `GET /api/lots`, zamknięcie jednej transzy: `POST /api/orders {"lot_id": N}`.

- Komercyjne API Yahoo jest nieudokumentowane i może się zmienić bez uprzedzenia.
- Brak wsparciu real-time dla GPW bez klucza brokera (patrz wyżej).
- Backtesting: symulacja krok po kroku, wypełnienie po otwarciu następnego
  dnia (nie po cenie zamykającej z dnia sygnału), stop/cel na dotkniętym
  poziomie, te same prowizje i slippage co w paper tradingu. Brak walk-forward.
- `execution.check_stops()` sprawdza ceny z ostatniego odczytu, nie
  sub-sekundowy tick. Na real-time GPW byłoby zbyt rzadkie.
- Backtest działa na dziennych świecach i danych do 2 lat wstecz. Brak
  walk-forward i walidacji out-of-sample — ranking konfiguracji jest
  optymistyczny, bo liczony na tych samych danych.
- Prowizja w backteście jest stała (0,35% + minimum). Brokerzy GPW stosują
  progi wolumenu, więc przy dużym wolumenie realny koszt jest niższy.
- `quoteSummary` wymaga handshake cookie+crumb; przy jego awarii panel
  „Dane spółki" pokazuje „brak danych", a ceny działają dalej.
