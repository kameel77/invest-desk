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

**Rewersja potwierdzona na dłuższym horyzoncie** (8 fałdów, 2 lata treningu
+ 5 mies. testu): **+0,67% expectancy na transakcję, 5/8 fałdów dodatnich**,
a wybrane parametry stabilne (7/8 razy `lookback=20, z_entry=2.0, hold=10`).
Kandydat do wdrożenia — ale z zastrzeżeniem, że przewaga zanika między
treningiem a testem (train ~1,0% → test ~0,3%).

Momentum krótkie przegrywa wyraźnie: -0,426% na transakcję przy 3 900
transakcjach, 0/4 fałdów zyskownych.

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
| `TD_STARTING_CASH` | `10000` | Kapitał startowy ( PLN) |
| `TD_COMMISSION_PCT` | `0.0035` | Prowizja % (GPW retail) |
| `TD_COMMISSION_MIN` | `1.00` | Prowizja minimalna |
| `TD_SLIPPAGE_PCT` | `0.001` | Slippage |
| `TD_MIN_TICKET` | `50` | Minimalny nominal zlecenia |
| `TD_SCAN_INTERVAL` | `300` | Interwał automatycznego skanu (s) |
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
