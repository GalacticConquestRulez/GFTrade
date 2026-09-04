"""
SQLite persistence for safety verdicts, so a restart doesn't throw away
everything the scanner has already proved.

Without this the cache is process memory: every `systemctl restart` makes
the bot re-vet the whole candidate pool from scratch, which is both slow
and a pile of wasted API credits — and restarts are frequent while tuning.

The stored TTLs are the same ones the in-memory cache uses (short for
incomplete reports so transient failures retry soon, long for complete
ones). Expired rows are ignored on load rather than trusted, so a stale
verdict can never resurface as current.

Synchronous sqlite3, matching factors.py: single process, tiny writes.
"""
import logging
import sqlite3
import time

from . import config
from .discovery.safety import SafetyReport

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS safety (
    mint TEXT PRIMARY KEY,
    fetched_at REAL NOT NULL,
    ttl REAL NOT NULL,
    decimals INTEGER,
    mint_renounced INTEGER,
    freeze_none INTEGER,
    top10_pct REAL,
    lp_locked_pct REAL,
    lp_source TEXT,
    standard_token INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_safety_fetched ON safety(fetched_at);
"""

# Columns that round-trip as tri-state booleans: True / False / unknown.
# Storing them as 1/0/NULL keeps "unknown" distinct from "False", which is
# the whole basis of the evidence rules — conflating them would turn an
# unverified coin into a condemned one, or worse, a trusted one.
_BOOL_FIELDS = ("mint_renounced", "freeze_none", "standard_token")
_REAL_FIELDS = ("top10_pct", "lp_locked_pct")


def _to_db(value):
    return None if value is None else int(bool(value))


def _from_db(value):
    return None if value is None else bool(value)


class SafetyCacheStore:
    def __init__(self, path: str = None):
        self.path = path or config.SAFETY_CACHE_DB
        self._db = sqlite3.connect(self.path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def load(self) -> dict:
        """{mint: (SafetyReport, fetched_at, ttl)} for rows still live.
        Expired rows are skipped, never revived."""
        now = time.time()
        out = {}
        try:
            rows = self._db.execute("SELECT * FROM safety").fetchall()
        except sqlite3.Error:
            logger.exception("safety cache read failed; starting cold")
            return {}
        for row in rows:
            if now - row["fetched_at"] >= row["ttl"]:
                continue
            report = SafetyReport(
                mint=row["mint"],
                decimals=row["decimals"],
                top10_pct=row["top10_pct"],
                lp_locked_pct=row["lp_locked_pct"],
                lp_source=row["lp_source"],
                error=row["error"],
                **{f: _from_db(row[f]) for f in _BOOL_FIELDS},
            )
            out[row["mint"]] = (report, row["fetched_at"], row["ttl"])
        return out

    def put(self, report, fetched_at: float, ttl: float) -> None:
        try:
            self._db.execute(
                "INSERT OR REPLACE INTO safety (mint, fetched_at, ttl, decimals,"
                " mint_renounced, freeze_none, top10_pct, lp_locked_pct,"
                " lp_source, standard_token, error)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (report.mint, fetched_at, ttl, report.decimals,
                 _to_db(report.mint_renounced), _to_db(report.freeze_none),
                 report.top10_pct, report.lp_locked_pct, report.lp_source,
                 _to_db(report.standard_token), report.error),
            )
            self._db.commit()
        except sqlite3.Error:
            logger.exception("safety cache write failed for %s", report.mint)

    def prune(self) -> int:
        """Drop rows whose TTL has elapsed. Returns how many went."""
        try:
            cursor = self._db.execute(
                "DELETE FROM safety WHERE ? - fetched_at >= ttl", (time.time(),))
            self._db.commit()
            return cursor.rowcount or 0
        except sqlite3.Error:
            logger.exception("safety cache prune failed")
            return 0

    def close(self) -> None:
        try:
            self._db.close()
        except sqlite3.Error:
            pass
