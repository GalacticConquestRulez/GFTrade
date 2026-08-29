"""
SQLite-backed factor log: a snapshot of every candidate the scanner
evaluates — passed OR failed, traded or not — so analysis.py can rank
which factors actually predict outcomes across the whole field instead of
just the coins that happened to alert.

Two outcome channels close the loop:
- price checkpoints 1h/6h/24h after each snapshot (filled by the scanner
  from batched lookups; a market that vanishes records price 0 = -100%),
- real trade results attached when a position opened on a logged coin
  closes (trade_result / trade_pnl_pct).

Snapshots are deduped per mint (default: one per 30 minutes) — the
scanner re-evaluates the same coin every tick and logging every glance
would just weight the dataset toward long-lived coins.

Synchronous sqlite3 is fine here: single process, tiny writes, and the
event loop already tolerates the JSON store's writes.
"""
import sqlite3
import time

from . import config

# factor columns extracted from a verdict, in insertion order
FACTOR_COLUMNS = [
    "liquidity_usd", "market_cap", "liq_mcap_ratio", "age_hours",
    "buys_5m", "sells_5m", "buy_sell_ratio_5m", "buys_h1", "sells_h1",
    "vol_5m", "vol_h1", "surge_ratio", "chg_5m", "chg_h1", "chg_h6",
    "pattern_conf", "score", "extension_pct",
]

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    mint TEXT NOT NULL,
    symbol TEXT,
    pattern TEXT,
    screened_ok INTEGER,
    safety_ok INTEGER,
    {", ".join(f"{col} REAL" for col in FACTOR_COLUMNS)},
    price0 REAL,
    p_h1 REAL, p_h6 REAL, p_h24 REAL,
    trade_result TEXT,
    trade_pnl_pct REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_mint_ts ON snapshots (mint, ts);
"""

HORIZONS = {"p_h1": 3600, "p_h6": 6 * 3600, "p_h24": 24 * 3600}
DEAD_GRACE = 2 * 3600


def extract_factors(verdict: dict) -> dict:
    """Pull the factor values out of a scanner verdict, tolerating the
    missing/null fields DexScreener serves on very new pairs."""
    from .discovery import filters  # local import avoids a cycle

    pair = verdict.get("pair") or {}
    liquidity = (pair.get("liquidity") or {}).get("usd") or 0
    market_cap = pair.get("marketCap") or pair.get("fdv") or 0
    txns = pair.get("txns") or {}
    m5, h1 = (txns.get("m5") or {}), (txns.get("h1") or {})
    volume = pair.get("volume") or {}
    change = pair.get("priceChange") or {}
    vol_5m = volume.get("m5") or 0
    vol_h1 = volume.get("h1") or 0
    sells_5m = m5.get("sells") or 0
    patterns = verdict.get("patterns") or []
    return {
        "liquidity_usd": liquidity,
        "market_cap": market_cap,
        "liq_mcap_ratio": (liquidity / market_cap) if market_cap else None,
        "age_hours": max(filters.pair_age_hours(pair), 0),
        "buys_5m": m5.get("buys") or 0,
        "sells_5m": sells_5m,
        "buy_sell_ratio_5m": (m5.get("buys") or 0) / max(sells_5m, 1),
        "buys_h1": h1.get("buys") or 0,
        "sells_h1": h1.get("sells") or 0,
        "vol_5m": vol_5m,
        "vol_h1": vol_h1,
        "surge_ratio": (vol_5m / (vol_h1 / 12)) if vol_h1 else None,
        "chg_5m": change.get("m5"),
        "chg_h1": change.get("h1"),
        "chg_h6": change.get("h6"),
        "pattern_conf": patterns[0]["confidence"] if patterns else 0.0,
        "score": verdict.get("score"),
        "extension_pct": verdict.get("extension_pct"),
    }


class FactorLog:
    def __init__(self, path: str = None):
        self.path = path or config.FACTOR_DB
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # ---------- writes ----------

    def log_snapshot(self, verdict: dict, dedupe_minutes: float = None):
        """Insert a snapshot for this verdict; returns the row id. Within
        the dedupe window the existing row's id is returned instead."""
        dedupe_minutes = (config.FACTOR_DEDUPE_MINUTES
                          if dedupe_minutes is None else dedupe_minutes)
        mint = verdict.get("mint")
        pair = verdict.get("pair") or {}
        price0 = float(pair.get("priceUsd") or 0)
        if not mint or price0 <= 0:
            return None
        recent = self._db.execute(
            "SELECT id FROM snapshots WHERE mint = ? AND ts > ? "
            "ORDER BY ts DESC LIMIT 1",
            (mint, time.time() - dedupe_minutes * 60),
        ).fetchone()
        if recent:
            return recent["id"]

        factors = extract_factors(verdict)
        columns = ["ts", "mint", "symbol", "pattern", "screened_ok",
                   "safety_ok", *FACTOR_COLUMNS, "price0"]
        patterns = verdict.get("patterns") or []
        values = [
            time.time(), mint,
            (pair.get("baseToken") or {}).get("symbol"),
            patterns[0]["pattern"] if patterns else None,
            int(bool(verdict.get("screened_ok"))),
            int(bool(verdict.get("safety_ok"))),
            *[factors[col] for col in FACTOR_COLUMNS],
            price0,
        ]
        cursor = self._db.execute(
            f"INSERT INTO snapshots ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' * len(columns))})", values,
        )
        self._db.commit()
        return cursor.lastrowid

    def latest_id_for_mint(self, mint: str, max_age_hours: float = 24):
        row = self._db.execute(
            "SELECT id FROM snapshots WHERE mint = ? AND ts > ? "
            "ORDER BY ts DESC LIMIT 1",
            (mint, time.time() - max_age_hours * 3600),
        ).fetchone()
        return row["id"] if row else None

    def update_trade_outcome(self, row_id, result: str, pnl_pct: float) -> None:
        if row_id is None:
            return
        self._db.execute(
            "UPDATE snapshots SET trade_result = ?, trade_pnl_pct = ? WHERE id = ?",
            (result, pnl_pct, row_id),
        )
        self._db.commit()

    # ---------- price checkpoints ----------

    def due_checkpoint_mints(self, limit: int = 30) -> list:
        now = time.time()
        clauses = " OR ".join(
            f"({col} IS NULL AND ts <= {now - delta})"
            for col, delta in HORIZONS.items()
        )
        rows = self._db.execute(
            f"SELECT DISTINCT mint FROM snapshots WHERE {clauses} LIMIT ?",
            (limit,),
        ).fetchall()
        return [row["mint"] for row in rows]

    def fill_checkpoints(self, prices: dict) -> int:
        """`prices`: mint -> current price. Fills every due checkpoint on
        every snapshot; a mint absent from `prices` records 0 (= -100%)
        once past the grace window."""
        now = time.time()
        filled = 0
        for col, delta in HORIZONS.items():
            rows = self._db.execute(
                f"SELECT id, mint, ts FROM snapshots WHERE {col} IS NULL AND ts <= ?",
                (now - delta,),
            ).fetchall()
            for row in rows:
                price = prices.get(row["mint"])
                if price is not None and price > 0:
                    self._db.execute(
                        f"UPDATE snapshots SET {col} = ? WHERE id = ?",
                        (price, row["id"]),
                    )
                    filled += 1
                elif now >= row["ts"] + delta + DEAD_GRACE:
                    self._db.execute(
                        f"UPDATE snapshots SET {col} = 0 WHERE id = ?", (row["id"],)
                    )
                    filled += 1
        if filled:
            self._db.commit()
        return filled

    # ---------- reads for analysis ----------

    def all_rows(self) -> list:
        return [dict(row) for row in
                self._db.execute("SELECT * FROM snapshots").fetchall()]

    def counts(self) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN trade_result IS NOT NULL THEN 1 ELSE 0 END) AS traded, "
            "SUM(CASE WHEN p_h24 IS NOT NULL THEN 1 ELSE 0 END) AS resolved_24h "
            "FROM snapshots"
        ).fetchone()
        return {key: row[key] or 0 for key in row.keys()}
