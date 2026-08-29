"""
Persistent state: runtime settings, open positions, trade history, alert
bookkeeping. One JSON file, written atomically (temp file + rename) so a
crash mid-write can't corrupt it.

Everything runs on one asyncio loop in one process, so no cross-process
locking is needed — but handlers and the scanner share this object, which
is why there is exactly one Store instance (created in main.py) instead of
each caller re-reading the file.
"""
import copy
import json
import os
import tempfile
import time

from . import config

_SCHEMA = {
    "settings": {},          # seeded from config.DEFAULT_SETTINGS on load
    "positions": {},         # mint -> position dict (see trading/engine.py)
    "closed_trades": [],
    "alerts": {},            # mint -> last alert unix ts (cooldown tracking)
    "muted": {},             # mint -> True (never alert again)
    "signal_log": [],        # signal outcomes for the report card (scanner.py)
    "stats": {
        "realized_pnl_sol": 0.0,
        "sim_balance_sol": config.SIM_START_BALANCE_SOL,
    },
}


class Store:
    def __init__(self, path: str = None):
        self.path = path or config.STATE_FILE
        self.data = self._load()

    def _load(self) -> dict:
        data = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
        # Merge schema so upgrades that add keys don't crash old state files.
        merged = copy.deepcopy(_SCHEMA)
        merged["settings"] = dict(config.DEFAULT_SETTINGS)
        for key, value in data.items():
            if key in ("settings", "stats"):
                merged[key].update(value)
            else:
                merged[key] = value
        return merged

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    # ---------- settings ----------

    @property
    def settings(self) -> dict:
        return self.data["settings"]

    def set_setting(self, key: str, value) -> None:
        if key not in config.DEFAULT_SETTINGS:
            raise KeyError(f"unknown setting: {key}")
        self.data["settings"][key] = value
        self.save()

    # ---------- positions ----------

    @property
    def positions(self) -> dict:
        return self.data["positions"]

    def get_position(self, mint: str):
        return self.data["positions"].get(mint)

    def put_position(self, position: dict) -> None:
        self.data["positions"][position["mint"]] = position
        self.save()

    def close_position(self, mint: str, trade: dict) -> None:
        self.data["positions"].pop(mint, None)
        self.data["closed_trades"].append(trade)
        self.data["stats"]["realized_pnl_sol"] += trade.get("pnl_sol", 0.0)
        self.save()

    # ---------- stats / paper wallet ----------

    @property
    def stats(self) -> dict:
        return self.data["stats"]

    def sim_adjust_balance(self, delta_sol: float) -> None:
        self.data["stats"]["sim_balance_sol"] += delta_sol
        self.save()

    # ---------- alert bookkeeping ----------

    def record_alert(self, mint: str) -> None:
        self.data["alerts"][mint] = time.time()
        # keep the map from growing forever
        cutoff = time.time() - 7 * 24 * 3600
        self.data["alerts"] = {m: ts for m, ts in self.data["alerts"].items() if ts > cutoff}
        self.save()

    def recently_alerted(self, mint: str, hours: float = None) -> bool:
        hours = hours if hours is not None else config.ALERT_COOLDOWN_HOURS
        ts = self.data["alerts"].get(mint)
        return ts is not None and (time.time() - ts) < hours * 3600

    # ---------- signal report card ----------

    SIGNAL_LOG_CAP = 300

    @property
    def signal_log(self) -> list:
        return self.data["signal_log"]

    def add_signal(self, entry: dict) -> None:
        self.data["signal_log"].append(entry)
        if len(self.data["signal_log"]) > self.SIGNAL_LOG_CAP:
            self.data["signal_log"] = self.data["signal_log"][-self.SIGNAL_LOG_CAP:]
        self.save()

    def mute(self, mint: str) -> None:
        self.data["muted"][mint] = True
        self.save()

    def is_muted(self, mint: str) -> bool:
        return bool(self.data["muted"].get(mint))

    # ---------- summary ----------

    def summary(self) -> dict:
        trades = self.data["closed_trades"]
        wins = sum(1 for t in trades if t.get("pnl_sol", 0) > 0)
        return {
            "open_positions": len(self.data["positions"]),
            "closed_trades": len(trades),
            "win_rate": (wins / len(trades)) if trades else None,
            "realized_pnl_sol": self.data["stats"]["realized_pnl_sol"],
            "sim_balance_sol": self.data["stats"]["sim_balance_sol"],
        }
