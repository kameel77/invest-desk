"""SQLite persistence layer.

One file, plain SQL, WAL mode. The schema is intentionally boring: candles,
signals, orders, fills, equity snapshots and the watchlist.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterable, Optional

from . import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles (
    symbol TEXT NOT NULL,
    ts     INTEGER NOT NULL,      -- epoch seconds, bar close time (UTC)
    open   REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS idx_candles_symbol_ts ON candles(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS quotes (
    symbol     TEXT PRIMARY KEY,
    ts         INTEGER NOT NULL,
    price      REAL, prev_close REAL, change_pct REAL,
    currency   TEXT, exchange TEXT, source TEXT
);

CREATE TABLE IF NOT EXISTS signals (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    symbol    TEXT NOT NULL,
    side      TEXT NOT NULL,       -- BUY | SELL
    score     REAL NOT NULL,
    price     REAL,
    strategy  TEXT,
    reasons   TEXT,                -- JSON list
    meta      TEXT,                -- JSON dict
    acted     INTEGER DEFAULT 0,
    closed_ts INTEGER
);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS orders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,     -- BUY | SELL
    qty         REAL NOT NULL,
    limit_price REAL,
    status      TEXT NOT NULL,     -- FILLED | REJECTED
    reason      TEXT,
    signal_id   INTEGER,
    realized_pnl REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS positions (
    symbol      TEXT PRIMARY KEY,
    qty         REAL NOT NULL,
    avg_price   REAL NOT NULL,
    opened_ts   INTEGER,
    stop_price  REAL,
    take_price  REAL,
    realized_pnl REAL DEFAULT 0
);

-- Lots: one row per individual transaction. A position is the SUM of its open
-- lots, never a merged average — so every entry keeps its own entry price,
-- stop, target and P&L, and the result of a decision stays attributable to
-- that decision instead of to the average of two calls. This is also the
-- shape Polish PIT needs: taxable gains are computed per disposed lot, not
-- per aggregate position.
CREATE TABLE IF NOT EXISTS lots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    hypothesis   TEXT NOT NULL DEFAULT 'trend',
    opened_ts    INTEGER NOT NULL,
    qty          REAL NOT NULL,
    entry_price  REAL NOT NULL,
    entry_fee    REAL DEFAULT 0,
    stop_price   REAL,
    take_price   REAL,
    entry_meta   TEXT,
    closed_ts    INTEGER,
    exit_price   REAL,
    exit_fee     REAL,
    exit_reason  TEXT,
    signal_id    INTEGER,
    realized_pnl REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_lots_open ON lots(symbol, closed_ts);
CREATE INDEX IF NOT EXISTS idx_lots_closed ON lots(closed_ts DESC);

-- Decision journal: append-only. Every judgement the system makes is written
-- here with the state that justified it, whether or not it became an order.
-- Signals and lots answer "what happened"; this answers "what did it think,
-- and why" — including the calls that were declined, which never appear in
-- a trade log. Nothing in normal operation deletes from this table.
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    symbol      TEXT,
    hypothesis  TEXT NOT NULL DEFAULT 'trend',
    action      TEXT NOT NULL,      -- SIGNAL | FILLED | REJECTED | EXIT | SKIPPED
    side        TEXT,               -- BUY | SELL | null
    price       REAL,
    score       REAL,
    qty         REAL,
    reason      TEXT,
    reasons     TEXT,               -- JSON array, the human-readable case
    state       TEXT,               -- JSON: cash, equity, open lots, z, RSI
    ref_type    TEXT,               -- signal | lot | order
    ref_id      INTEGER,
    realized_pnl REAL
);
CREATE INDEX IF NOT EXISTS idx_decisions_ts ON decisions(ts DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_symbol ON decisions(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS equity (
    ts     INTEGER PRIMARY KEY,
    cash   REAL, equity REAL, day_pnl REAL
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol  TEXT PRIMARY KEY,
    added_ts INTEGER NOT NULL,
    note    TEXT
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def get_conn() -> sqlite3.Connection:
    """Thread-local connection (FastAPI runs handlers on a worker pool)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()


# --- candles --------------------------------------------------------------

def upsert_candles(symbol: str, rows: Iterable[tuple]) -> int:
    """``rows`` = iterable of (ts, o, h, l, c, v). Returns rows written."""
    payload = [(symbol, int(ts), o, h, l, c, v) for ts, o, h, l, c, v in rows]
    if not payload:
        return 0
    with tx() as conn:
        cur = conn.executemany(
            "INSERT INTO candles(symbol, ts, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol, ts) DO UPDATE SET "
            "open=excluded.open, high=excluded.high, low=excluded.low, "
            "close=excluded.close, volume=excluded.volume",
            payload,
        )
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(payload)


def get_candles(symbol: str, limit: int = 400) -> list[sqlite3.Row]:
    cur = get_conn().execute(
        "SELECT ts, open, high, low, close, volume FROM candles "
        "WHERE symbol=? ORDER BY ts DESC LIMIT ?",
        (symbol.upper(), limit),
    )
    return list(cur.fetchall())[::-1]


def last_candle_ts(symbol: str) -> Optional[int]:
    row = get_conn().execute(
        "SELECT MAX(ts) AS ts FROM candles WHERE symbol=?", (symbol.upper(),)
    ).fetchone()
    return row["ts"] if row and row["ts"] is not None else None


# --- quotes ---------------------------------------------------------------

def save_quote(symbol: str, q: dict[str, Any]) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO quotes(symbol, ts, price, prev_close, change_pct, currency, exchange, source) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET ts=excluded.ts, price=excluded.price, "
            "prev_close=excluded.prev_close, change_pct=excluded.change_pct, "
            "currency=excluded.currency, exchange=excluded.exchange, source=excluded.source",
            (symbol.upper(), int(q.get("ts") or 0), q.get("price"), q.get("prev_close"),
             q.get("change_pct"), q.get("currency"), q.get("exchange"), q.get("source")),
        )


def get_quotes(symbols: Optional[list[str]] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if symbols:
        marks = ",".join("?" * len(symbols))
        return list(conn.execute(
            f"SELECT * FROM quotes WHERE symbol IN ({marks})", [s.upper() for s in symbols]))
    return list(conn.execute("SELECT * FROM quotes"))


# --- signals --------------------------------------------------------------

def add_signal(sig: dict[str, Any]) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO signals(ts, symbol, side, score, price, strategy, reasons, meta) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (int(sig["ts"]), sig["symbol"].upper(), sig["side"].upper(), float(sig["score"]),
             sig.get("price"), sig.get("strategy", ""),
             json.dumps(sig.get("reasons", []), ensure_ascii=False),
             json.dumps(sig.get("meta", {}), ensure_ascii=False)),
        )
        return int(cur.lastrowid or 0)


def recent_signals(limit: int = 100, acted: Optional[bool] = None) -> list[dict]:
    conn = get_conn()
    sql = "SELECT * FROM signals"
    args: list[Any] = []
    if acted is not None:
        sql += " WHERE acted=?"
        args.append(1 if acted else 0)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in conn.execute(sql, args):
        d = dict(r)
        for key, empty in (("reasons", []), ("meta", {})):
            try:
                d[key] = json.loads(d[key]) if d.get(key) else empty
            except Exception:
                d[key] = empty
        out.append(d)
    return out


def mark_signal_acted(signal_id: int) -> None:
    with tx() as conn:
        conn.execute("UPDATE signals SET acted=1 WHERE id=?", (signal_id,))


# --- orders / positions ---------------------------------------------------

def get_position(symbol: str) -> Optional[sqlite3.Row]:
    return get_conn().execute(
        "SELECT * FROM positions WHERE symbol=?", (symbol.upper(),)).fetchone()


def all_positions() -> list[sqlite3.Row]:
    return list(get_conn().execute("SELECT * FROM positions WHERE qty > 0"))


def upsert_position(symbol: str, qty: float, avg_price: float, opened_ts: int,
                    stop_price: Optional[float] = None, take_price: Optional[float] = None,
                    realized_pnl: float = 0.0) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO positions(symbol, qty, avg_price, opened_ts, stop_price, take_price, realized_pnl) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(symbol) DO UPDATE SET qty=excluded.qty, avg_price=excluded.avg_price, "
            "stop_price=excluded.stop_price, take_price=excluded.take_price, realized_pnl=excluded.realized_pnl",
            (symbol.upper(), qty, avg_price, opened_ts, stop_price, take_price, realized_pnl),
        )


def close_position(symbol: str) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM positions WHERE symbol=?", (symbol.upper(),))


# --- lots ----------------------------------------------------------------

def open_lot(symbol: str, qty: float, entry_price: float, entry_fee: float,
             stop: Optional[float], target: Optional[float],
             hypothesis: str = "trend", entry_meta: str = "",
             signal_id: Optional[int] = None) -> int:
    """Record one transaction. Never merges with an existing one."""
    with tx() as c:
        cur = c.execute(
            "INSERT INTO lots(symbol, hypothesis, opened_ts, qty, entry_price, "
            "entry_fee, stop_price, take_price, entry_meta, signal_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (symbol.upper(), hypothesis, int(time.time()), qty, entry_price,
             entry_fee, stop, target, entry_meta or "", signal_id))
        return int(cur.lastrowid or 0)


def close_lot(lot_id: int, exit_price: float, exit_fee: float,
              reason: str, realized_pnl: float) -> None:
    with tx() as c:
        c.execute(
            "UPDATE lots SET closed_ts=?, exit_price=?, exit_fee=?, "
            "exit_reason=?, realized_pnl=? WHERE id=?",
            (int(time.time()), exit_price, exit_fee, reason,
             round(realized_pnl, 4), lot_id))


def open_lots(symbol: Optional[str] = None,
              hypothesis: Optional[str] = None) -> list[sqlite3.Row]:
    """Open lots, oldest first — FIFO is the default disposal order for PIT."""
    sql = "SELECT * FROM lots WHERE closed_ts IS NULL"
    args: list[Any] = []
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol.upper())
    if hypothesis:
        sql += " AND hypothesis=?"
        args.append(hypothesis)
    sql += " ORDER BY opened_ts ASC, id ASC"
    return list(get_conn().execute(sql, args))


def closed_lots(limit: int = 200) -> list[dict[str, Any]]:
    out = []
    for r in get_conn().execute(
            "SELECT * FROM lots WHERE closed_ts IS NOT NULL "
            "ORDER BY closed_ts DESC LIMIT ?", (limit,)).fetchall():
        d = dict(r)
        d["entry_meta"] = _load_json(d.get("entry_meta"), {})
        out.append(d)
    return out


def lot_stats(limit_days: Optional[int] = None) -> dict[str, Any]:
    """Per-hypothesis performance of closed lots — the per-transaction view."""
    sql = ("SELECT hypothesis, COUNT(*) n, "
           "SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) wins, "
           "AVG(realized_pnl) avg_pnl, SUM(realized_pnl) total_pnl "
           "FROM lots WHERE closed_ts IS NOT NULL")
    args: list[Any] = []
    if limit_days:
        sql += " AND closed_ts >= ?"
        args.append(int(time.time()) - limit_days * 86400)
    sql += " GROUP BY hypothesis"
    out = []
    for r in get_conn().execute(sql, args):
        n = r["n"] or 0
        out.append({
            "hypothesis": r["hypothesis"],
            "trades": n,
            "wins": r["wins"] or 0,
            "hit_rate_pct": round((r["wins"] or 0) / n * 100, 1) if n else 0.0,
            "avg_pnl": round(r["avg_pnl"] or 0.0, 2),
            "total_pnl": round(r["total_pnl"] or 0.0, 2),
        })
    return {"by_hypothesis": out}


def _load_json(raw: Any, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except Exception:
        return default


def record_decision(**kw: Any) -> int:
    """Append one entry to the decision journal. Never updated, never deleted."""
    with tx() as c:
        cur = c.execute(
            "INSERT INTO decisions (ts, symbol, hypothesis, action, side, price, "
            "score, qty, reason, reasons, state, ref_type, ref_id, realized_pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(kw.get("ts") or time.time()),
             (kw.get("symbol") or "").upper() or None,
             kw.get("hypothesis", "trend"), kw["action"],
             kw.get("side"), kw.get("price"), kw.get("score"), kw.get("qty"),
             kw.get("reason"),
             json.dumps(kw["reasons"], ensure_ascii=False) if kw.get("reasons") else None,
             json.dumps(kw["state"], ensure_ascii=False, default=str) if kw.get("state") else None,
             kw.get("ref_type"), kw.get("ref_id"), kw.get("realized_pnl")))
        return int(cur.lastrowid or 0)


def recent_decisions(limit: int = 200, symbol: Optional[str] = None,
                     action: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM decisions WHERE 1=1"
    args: list[Any] = []
    if symbol:
        sql += " AND symbol=?"
        args.append(symbol.upper())
    if action:
        sql += " AND action=?"
        args.append(action)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    args.append(limit)
    out = []
    for r in get_conn().execute(sql, args):
        d = dict(r)
        d["reasons"] = _load_json(d.get("reasons"), [])
        d["state"] = _load_json(d.get("state"), {})
        out.append(d)
    return out


def add_order(order: dict[str, Any]) -> int:
    with tx() as conn:
        cur = conn.execute(
            "INSERT INTO orders(ts, symbol, side, qty, limit_price, status, reason, signal_id, realized_pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (int(order["ts"]), order["symbol"].upper(), order["side"].upper(), float(order["qty"]),
             order.get("limit_price"), order.get("status", "FILLED"), order.get("reason"),
             order.get("signal_id"), float(order.get("realized_pnl", 0.0))),
        )
        return int(cur.lastrowid or 0)


def recent_orders(limit: int = 100) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT * FROM orders ORDER BY ts DESC LIMIT ?", (limit,))]


# --- cash / equity --------------------------------------------------------

def get_cash() -> float:
    row = get_conn().execute("SELECT v FROM kv WHERE k='cash'").fetchone()
    if row is None:
        with tx() as conn:
            conn.execute("INSERT INTO kv(k, v) VALUES ('cash', ?)", (str(config.STARTING_CASH),))
        return config.STARTING_CASH
    return float(row["v"])


def set_cash(value: float) -> None:
    with tx() as conn:
        conn.execute("INSERT INTO kv(k, v) VALUES ('cash', ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                     (str(float(value)),))


def snapshot_equity(equity: float, day_pnl: float) -> None:
    with tx() as conn:
        conn.execute(
            "INSERT INTO equity(ts, cash, equity, day_pnl) VALUES (?,?,?,?) "
            "ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity, day_pnl=excluded.day_pnl",
            (int(time.time()), get_cash(), equity, day_pnl),
        )


def equity_curve(limit: int = 500) -> list[dict]:
    return [dict(r) for r in get_conn().execute(
        "SELECT ts, cash, equity, day_pnl FROM equity ORDER BY ts DESC LIMIT ?", (limit,))][::-1]


# --- watchlist ------------------------------------------------------------

def add_to_watchlist(symbol: str, note: str = "") -> None:
    with tx() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO watchlist(symbol, added_ts, note) VALUES (?,?,?)",
            (symbol.upper(), int(time.time()), note),
        )


def remove_from_watchlist(symbol: str) -> None:
    with tx() as conn:
        conn.execute("DELETE FROM watchlist WHERE symbol=?", (symbol.upper(),))


def get_watchlist() -> list[str]:
    rows = get_conn().execute("SELECT symbol FROM watchlist ORDER BY symbol").fetchall()
    if rows:
        return [r["symbol"] for r in rows]
    for s in config.DEFAULT_WATCHLIST:
        add_to_watchlist(s)
    return list(config.DEFAULT_WATCHLIST)
