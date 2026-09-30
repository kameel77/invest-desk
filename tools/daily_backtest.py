"""Daily signal-testing job.

Runs the sweep over the whole watchlist, stores the result in SQLite, and
writes a human-readable markdown report. This is what answers "how do I tune
the model": every day the same grid runs on fresh data, so a change that
helps shows up as a sustained improvement across runs rather than a single
lucky backtest.

Run manually:      .venv/bin/python -m tools.daily_backtest
Run via cron:     ./run_daily.sh
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app import backtest as bt
from app import db, market, walkforward as wf

REPORT_DIR = Path(__file__).resolve().parent.parent / "reports"


def _store(result: dict[str, Any]) -> None:
    with db.tx() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS backtest_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, symbols INTEGER, "
            "payload TEXT)")
        c.execute("INSERT INTO backtest_runs(ts, symbols, payload) VALUES (?,?,?)",
                  (int(result["ts"]), result["symbols"], json.dumps(result, ensure_ascii=False)))


def history(limit: int = 30) -> list[dict[str, Any]]:
    rows = db.get_conn().execute(
        "SELECT id, ts, symbols, payload FROM backtest_runs ORDER BY ts DESC LIMIT ?",
        (limit,)).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload"] or "{}")
        best = (payload.get("variants") or [{}])[0]
        out.append({
            "id": r["id"], "ts": r["ts"], "symbols": r["symbols"],
            "variants": len(payload.get("variants") or []),
            "best_label": best.get("label"),
            "best_return_pct": best.get("total_return_pct"),
            "best_sharpe": best.get("sharpe"),
        })
    return out


def _fmt(v: Any, suffix: str = "") -> str:
    if v is None:
        return "—"
    return f"{v}{suffix}"


def _store_walkforward(result: dict[str, Any]) -> None:
    with db.tx() as c:
        c.execute(
            "CREATE TABLE IF NOT EXISTS walkforward_runs ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, "
            "oos_return REAL, oos_sharpe REAL, profitable_folds INTEGER, "
            "folds INTEGER, baseline_return REAL, payload TEXT)")
        oos = result.get("oos", {})
        base = result.get("baseline_oos", {})
        c.execute(
            "INSERT INTO walkforward_runs(ts, oos_return, oos_sharpe, "
            "profitable_folds, folds, baseline_return, payload) "
            "VALUES (?,?,?,?,?,?,?)",
            (int(result["ts"]), oos.get("avg_return_pct", 0), oos.get("avg_sharpe", 0),
             oos.get("profitable_folds", 0), result.get("folds", 0),
             base.get("avg_return_pct", 0), json.dumps(result, ensure_ascii=False)))


def _append_walkforward_report(result: dict[str, Any]) -> None:
    """Insert the out-of-sample verdict into the day's markdown report."""
    oos, base = result["oos"], result["baseline_oos"]
    lines = [
        "",
        "## Walk-forward (jedyna uczciwa miara)",
        "",
        f"Konfiguracja wybierana na danych treningowych, oceniana na późniejszych, "
        f"niewidzianych słupkach. {result['folds']} fałdów, {result['candidates']} "
        f"kandydatów, {result['symbols']} instrumentów.",
        "",
        "| Fałd | Wybrana konfiguracja | Trening % | Train Sharpe | Test % | Test Sharpe | Bazowo % |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in result["results"]:
        t, te, d = f["train"], f["test"], f["test_default"]
        lines.append(
            f"| {f['fold']} | {f['chosen']} | {t['total_return_pct']} | "
            f"{t['sharpe']} | {te['total_return_pct']} | {te['sharpe']} | "
            f"{d['total_return_pct']} |")
    lines += [
        "",
        f"**Zarobki poza próbką: {oos['avg_return_pct']}%** "
        f"({oos['profitable_folds']}/{oos['folds']} fałdów zyskownych, "
        f"Sharpe {oos['avg_sharpe']}, {oos['trades']} transakcji)",
        "",
        f"Dla porównania — konfiguracja domyślna bez dostrajania: "
        f"{base['avg_return_pct']}% ({base['profitable_folds']}/{result['folds']} fałdów).",
    ]
    body = "\n".join(lines)
    stamp = datetime.fromtimestamp(result["ts"]).strftime("%Y-%m-%d_%H%M")
    for name in (f"backtest_{stamp}.md", "latest.md"):
        p = REPORT_DIR / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        if "## Walk-forward" in text:
            continue
        idx = text.find("## Wniosek")
        p.write_text((text[:idx] + body + "\n\n" + text[idx:]) if idx > 0
                     else text + "\n" + body + "\n", encoding="utf-8")


def write_report(result: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    stamp = datetime.fromtimestamp(result["ts"]).strftime("%Y-%m-%d_%H%M")
    path = REPORT_DIR / f"backtest_{stamp}.md"

    variants = result["variants"]
    lines = [
        f"# Raport testów sygnałów — {datetime.fromtimestamp(result['ts']).strftime('%Y-%m-%d %H:%M')}",
        "",
        f"**Uniwersum:** {result['symbols']} instrumentów · "
        f"**Wariantów:** {len(variants)}",
        "",
        "## Ranking konfiguracji",
        "",
        "| # | Konfiguracja | Transakcje | Zwrot | Trajność | PF | Max DD | Sharpe |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for i, v in enumerate(variants, 1):
        lines.append(
            f"| {i} | {v['label']} | {v['trades']} | {_fmt(v['total_return_pct'], '%')} | "
            f"{_fmt(v['hit_rate_pct'], '%')} | {_fmt(v['profit_factor'])} | "
            f"{_fmt(v['max_drawdown_pct'], '%')} | {_fmt(v['sharpe'])} |")

    best = variants[0] if variants else None
    lines += ["", "## Wniosek", ""]
    if best:
        lines += [
            f"Najlepsza konfiguracja: **{best['label']}** — "
            f"zwrot {best['total_return_pct']}%, Sharpe {best['sharpe']}, "
            f"max drawdown {best['max_drawdown_pct']}%.",
        ]
    else:
        lines.append("Brak wyników.")

    path.write_text("\n".join(lines), encoding="utf-8")
    # Stable pointer for the dashboard / cron readers.
    (REPORT_DIR / "latest.md").write_text("\n".join(lines), encoding="utf-8")
    (REPORT_DIR / "latest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


async def main() -> int:
    db.init_db()
    symbols = db.get_watchlist()
    # Benchmarks and macro series are context inputs, not signals — they must
    # be present or the regime/RS gate silently degrades to "no data".
    from app.context import BENCHMARKS, VIX_SYMBOL
    fetch = sorted(set(list(BENCHMARKS.values()) + [VIX_SYMBOL]))
    print(f"[{time.strftime('%H:%M:%S')}] Odświeżam kontekst rynkowy: {', '.join(fetch)}…")
    await market.refresh_candles(fetch, rng="2y")
    print(f"[{time.strftime('%H:%M:%S')}] Odświeżam dane dla {len(symbols)} instrumentów…")
    await market.refresh_candles(symbols, rng="2y")

    print(f"[{time.strftime('%H:%M:%S')}] Uruchamiam sweep parametrów…")
    result = await asyncio.to_thread(bt.sweep, symbols)
    _store(result)

    path = write_report(result)
    print(f"[{time.strftime('%H:%M:%S')}] Raport: {path}")

    variants = result["variants"]
    print("\nTOP 5 konfiguracji:")
    for v in variants[:5]:
        print(f"  {v['label']:42} ret={v['total_return_pct']:>7.2f}%  "
              f"trades={v['trades']:>4}  PF={v['profit_factor']}  sharpe={v['sharpe']}")

    profitable = [v for v in variants if (v["total_return_pct"] or 0) > 0]
    print(f"\nWariantów zyskownych: {len(profitable)}/{len(variants)}")

    # The sweep above ranks configurations on the bars it then scores them on;
    # walk-forward is the only number that answers "does it work at all".
    print("\nWalk-forward (dane treningowe -> niewidziane słupki)…")
    wfr = await asyncio.to_thread(wf.run, symbols, 5, 2.0, 6.0)
    if "error" in wfr:
        print("  walk-forward:", wfr["error"])
    else:
        _store_walkforward(wfr)
        oos, base = wfr["oos"], wfr["baseline_oos"]
        for f in wfr["results"]:
            t, te, d = f["train"], f["test"], f["test_default"]
            print(f"  F{f['fold']} {f['chosen'][:46]:46} "
                  f"train {t['total_return_pct']:>6}% sh={t['sharpe']:>5} | "
                  f"test {te['total_return_pct']:>6}% sh={te['sharpe']:>5} | "
                  f"bazowo {d['total_return_pct']:>6}%")
        print(f"\n  ZARÓBKI POZA PRÓBKĄ: {oos['avg_return_pct']}% "
              f"({oos['profitable_folds']}/{oos['folds']} fałdów, Sharpe {oos['avg_sharpe']})")
        print(f"  BAZOWO (bez dostrajania): {base['avg_return_pct']}% "
              f"({base['profitable_folds']}/{wfr['folds']} fałdów)")
        _append_walkforward_report(wfr)
    _ = bt.BacktestParams  # keep the import meaningful for readers
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
