# Raport testów sygnałów — 2026-09-29 20:00

**Uniwersum:** 52 instrumentów · **Wariantów:** 19

## Ranking konfiguracji

| # | Konfiguracja | Transakcje | Zwrot | Trajność | PF | Max DD | Sharpe |
|---|---|---|---|---|---|---|---|
| 1 | wej≥35 wyj≤-25 SL3.0 TP4.5 H20 | 844 | 13.97% | 53.8% | 1.06 | -11.68% | 0.44 |
| 2 | wej≥55 wyj≤-25 SL2.0 TP3.0 H20 | 180 | -1.51% | 46.7% | 0.98 | -10.39% | -0.1 |
| 3 | wej≥35 wyj≤-25 SL2.5 TP3.0 H20 | 1009 | -10.02% | 53.9% | 0.97 | -21.82% | -0.17 |
| 4 | wej≥35 wyj≤-25 SL2.0 TP3.0 H40 | 1018 | -10.29% | 47.2% | 0.97 | -32.61% | -0.11 |
| 5 | wej≥35 wyj≤-25 SL2.0 TP4.0 H20 | 1005 | -11.65% | 46.3% | 0.97 | -28.17% | -0.14 |
| 6 | wej≥45 wyj≤-25 SL2.0 TP3.0 H20 | 525 | -17.44% | 46.3% | 0.91 | -21.53% | -0.6 |
| 7 | wej≥35 wyj≤-25 SL2.0 TP3.0 H20 | 1103 | -25.88% | 48.7% | 0.93 | -37.97% | -0.37 |
| 8 | wej≥35 wyj≤-25 SL2.0 TP3.0 H20 | 1103 | -25.88% | 48.7% | 0.93 | -37.97% | -0.37 |
| 9 | wej≥35 wyj≤-25 SL2.0 TP3.0 H20 | 1103 | -25.88% | 48.7% | 0.93 | -37.97% | -0.37 |
| 10 | wej≥35 wyj≤-25 SL2.0 TP3.0 H20 | 1188 | -28.71% | 48.2% | 0.93 | -39.62% | -0.38 |
| 11 | wej≥15 wyj≤-25 SL2.0 TP3.0 H20 | 1892 | -35.58% | 48.9% | 0.94 | -42.73% | -0.39 |
| 12 | wej≥35 wyj≤-25 SL2.0 TP3.0 H20 | 1855 | -36.91% | 48.9% | 0.94 | -40.36% | -0.39 |
| 13 | wej≥35 wyj≤-25 SL2.0 TP3.0 H20 | 1481 | -39.37% | 48.5% | 0.93 | -48.13% | -0.43 |
| 14 | wej≥25 wyj≤-25 SL2.0 TP3.0 H20 | 1594 | -46.28% | 48.3% | 0.91 | -54.36% | -0.55 |
| 15 | wej≥35 wyj≤-25 SL2.0 TP3.0 H20 bez-rev | 1707 | -49.43% | 48.0% | 0.92 | -53.32% | -0.52 |
| 16 | wej≥35 wyj≤-25 SL2.0 TP3.0 H20 | 1423 | -53.61% | 47.9% | 0.89 | -60.02% | -0.71 |
| 17 | wej≥35 wyj≤-25 SL2.0 TP3.0 H10 | 1350 | -66.83% | 49.6% | 0.83 | -67.46% | -0.89 |
| 18 | wej≥35 wyj≤-25 SL1.5 TP2.0 H20 | 1505 | -118.61% | 46.2% | 0.77 | -119.08% | -0.65 |
| 19 | wej≥35 wyj≤-25 SL2.0 TP3.0 H5 | 1873 | -119.02% | 51.0% | 0.73 | -119.0% | -0.77 |


## Walk-forward (jedyna uczciwa miara)

Konfiguracja wybierana na danych treningowych, oceniana na późniejszych, niewidzianych słupkach. 5 fałdów, 9 kandydatów, 52 instrumentów.

| Fałd | Wybrana konfiguracja | Trening % | Train Sharpe | Test % | Test Sharpe | Bazowo % |
|---|---|---|---|---|---|---|
| 1 | wej≥35 wyj≤-25 SL3.0 TP4.5 H20 | -5.87 | -0.25 | -4.21 | -0.98 | -18.05 |
| 2 | wej≥35 wyj≤-25 SL3.0 TP4.5 H20 | 14.41 | 0.83 | 2.6 | 0.65 | 2.75 |
| 3 | wej≥35 wyj≤-25 SL3.0 TP4.5 H20 | -0.85 | -0.0 | 17.16 | 4.71 | 12.03 |
| 4 | wej≥35 wyj≤-25 SL3.0 TP4.5 H20 | 1.45 | 0.14 | -2.77 | -0.64 | -9.06 |
| 5 | wej≥35 wyj≤-25 SL3.0 TP4.5 H20 | 24.31 | 1.96 | -22.86 | -5.46 | -37.65 |

**Zarobki poza próbką: -2.02%** (2/5 fałdów zyskownych, Sharpe -0.34, 535 transakcji)

Dla porównania — konfiguracja domyślna bez dostrajania: -10.0% (2/5 fałdów).
## Wniosek

Najlepsza konfiguracja: **wej≥35 wyj≤-25 SL3.0 TP4.5 H20** — zwrot 13.97%, Sharpe 0.44, max drawdown -11.68%.