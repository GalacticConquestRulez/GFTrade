"""
The discovery loop — the "automating finding good coins" half of the bot.

Every tick:
  1. Positions first: update peaks, fire TP/SL/trailing exits.
  2. Pull DexScreener's latest token profiles (keyless proxy for "new
     token") and fold fresh Solana mints into a rolling candidate pool.
  3. Re-check the pool in batches. This matters: a token that was too
     young or too thin ten minutes ago gets re-evaluated every tick until
     it either qualifies or ages out — the moment-in-time feed alone would
     miss almost everything that matures between glances.
  4. Hard-screen survivors (filters), safety-check them on-chain, score
     them 0-100, and require at least one triggered pattern.
  5. Qualifiers become Telegram signal cards with buy buttons — or, if
     autobuy is enabled and the stricter autobuy score is met, positions.

Everything returned from tick() is a plain event dict; the Telegram layer
decides how to render each type.
"""
import asyncio
import logging
import time

from . import config, constants
from .discovery import filters, patterns, scoring

logger = logging.getLogger(__name__)

POOL_NO_PAIR_GRACE_HOURS = 3   # drop pool entries that never produce a tradable pair
EVAL_BATCH_PER_TICK = 120      # pool mints re-checked per tick (4 DexScreener calls)


class Scanner:
    def __init__(self, store, dex, engine, safety_checker):
        self.store = store
        self.dex = dex
        self.engine = engine
        self.safety = safety_checker
        # mint -> first_seen ts. In-memory only: after a restart the profile
        # feed repopulates it within a few ticks.
        self.pool = {}
        self.last_tick_at = None
        self.last_tick_stats = {}

    # ---------- pool management ----------

    def _absorb_profiles(self, profiles: list) -> int:
        added = 0
        for profile in profiles:
            if profile.get("chainId") != constants.CHAIN_ID:
                continue
            mint = profile.get("tokenAddress")
            if not mint or mint in self.pool:
                continue
            if self.store.is_muted(mint) or self.store.get_position(mint):
                continue
            self.pool[mint] = time.time()
            added += 1
        # Cap the pool, evicting the oldest entries first.
        if len(self.pool) > config.CANDIDATE_POOL_MAX:
            for mint, _ in sorted(self.pool.items(), key=lambda kv: kv[1])[
                : len(self.pool) - config.CANDIDATE_POOL_MAX
            ]:
                del self.pool[mint]
        return added

    # ---------- evaluation ----------

    async def evaluate_pair(self, pair: dict, boosted: set) -> dict:
        """Full verdict for one pair: screen -> safety -> patterns -> score."""
        ok, reasons = filters.screen_pair(pair, boosted)
        verdict = {
            "pair": pair,
            "mint": (pair.get("baseToken") or {}).get("address"),
            "screened_ok": ok,
            "reject_reasons": reasons,
            "safety": None,
            "patterns": [],
            "score": 0,
            "breakdown": {},
        }
        if not ok:
            return verdict
        strict = self.store.settings["security_strict"]
        verdict["safety"] = await self.safety.check(verdict["mint"])
        verdict["patterns"] = patterns.scan(pair)
        verdict["score"], verdict["breakdown"] = scoring.score_pair(
            pair, verdict["safety"], strict
        )
        return verdict

    def _qualifies(self, verdict: dict) -> bool:
        settings = self.store.settings
        return (
            verdict["screened_ok"]
            and verdict["patterns"]
            and verdict["score"] >= settings["min_alert_score"]
            and verdict["safety"] is not None
            and verdict["safety"].passes(settings["security_strict"])
        )

    # ---------- the tick ----------

    async def tick(self) -> list:
        events = []
        stats = {"profiles_new": 0, "pool": 0, "checked": 0, "passed_screen": 0,
                 "signals": 0}

        # 1. Manage what we already hold — exits take priority over entries.
        try:
            events.extend(await self.engine.check_exits())
        except Exception as exc:
            logger.exception("exit check failed")
            events.append({"type": "scan_error", "where": "check_exits", "error": str(exc)})

        settings = self.store.settings
        if settings["scanner_on"]:
            try:
                events.extend(await self._discover(stats))
            except Exception as exc:
                logger.exception("discovery failed")
                events.append({"type": "scan_error", "where": "discovery", "error": str(exc)})

        stats["pool"] = len(self.pool)
        self.last_tick_at = time.time()
        self.last_tick_stats = stats
        return events

    async def _discover(self, stats: dict) -> list:
        events = []
        settings = self.store.settings

        profiles = await self.dex.token_profiles_latest()
        stats["profiles_new"] = self._absorb_profiles(profiles)

        boosted = set()
        if config.EXCLUDE_BOOSTED:
            try:
                boosted = await self.dex.boosted_token_addresses()
            except Exception:
                logger.warning("boost feed unavailable; continuing without it")

        # Newest candidates first — they're the time-critical ones.
        batch = [m for m, _ in sorted(self.pool.items(), key=lambda kv: -kv[1])]
        batch = batch[:EVAL_BATCH_PER_TICK]
        if not batch:
            return events
        pairs = await self.dex.pairs_for_tokens(constants.CHAIN_ID, batch)

        best_by_mint = {}
        for pair in pairs:
            mint = (pair.get("baseToken") or {}).get("address")
            if mint in self.pool:
                current = best_by_mint.get(mint)
                liq = (pair.get("liquidity") or {}).get("usd") or 0
                if current is None or liq > ((current.get("liquidity") or {}).get("usd") or 0):
                    best_by_mint[mint] = pair

        now = time.time()
        for mint in batch:
            pair = best_by_mint.get(mint)
            if pair is None:
                if now - self.pool[mint] > POOL_NO_PAIR_GRACE_HOURS * 3600:
                    del self.pool[mint]
                continue
            age_hours = filters.pair_age_hours(pair)
            if age_hours > config.MAX_PAIR_AGE_HOURS:
                del self.pool[mint]  # aged out of our window for good
                continue

            stats["checked"] += 1
            verdict = await self.evaluate_pair(pair, boosted)
            if verdict["screened_ok"]:
                stats["passed_screen"] += 1
            if not self._qualifies(verdict):
                continue
            if self.store.is_muted(mint) or self.store.recently_alerted(mint):
                continue
            if self.store.get_position(mint):
                continue

            stats["signals"] += 1
            self.store.record_alert(mint)

            can_autobuy = (
                settings["autobuy"]
                and verdict["score"] >= settings["min_autobuy_score"]
                and len(self.store.positions) < settings["max_positions"]
            )
            if can_autobuy:
                try:
                    result = await self.engine.buy(
                        mint, settings["autobuy_sol"], source="auto", pair=pair
                    )
                    events.append({"type": "autobuy", "verdict": verdict, "result": result})
                    del self.pool[mint]
                    continue
                except Exception as exc:
                    events.append({"type": "autobuy_error", "verdict": verdict,
                                   "error": str(exc)})
            events.append({"type": "signal", "verdict": verdict})
        return events

    # ---------- on-demand scan (/scan) ----------

    async def scan_now(self, top_n: int = 5) -> list:
        """One synchronous sweep for the /scan command: refresh the pool,
        evaluate everything, return the best `top_n` verdicts that passed
        the hard screen — even below the alert threshold, so you can see
        what the scanner is weighing."""
        profiles = await self.dex.token_profiles_latest()
        self._absorb_profiles(profiles)
        boosted = set()
        if config.EXCLUDE_BOOSTED:
            try:
                boosted = await self.dex.boosted_token_addresses()
            except Exception:
                pass
        batch = [m for m, _ in sorted(self.pool.items(), key=lambda kv: -kv[1])]
        batch = batch[:EVAL_BATCH_PER_TICK]
        if not batch:
            return []
        pairs = await self.dex.pairs_for_tokens(constants.CHAIN_ID, batch)
        best_by_mint = {}
        for pair in pairs:
            mint = (pair.get("baseToken") or {}).get("address")
            current = best_by_mint.get(mint)
            liq = (pair.get("liquidity") or {}).get("usd") or 0
            if current is None or liq > ((current.get("liquidity") or {}).get("usd") or 0):
                best_by_mint[mint] = pair

        verdicts = []
        for mint, pair in best_by_mint.items():
            verdict = await self.evaluate_pair(pair, boosted)
            if verdict["screened_ok"]:
                verdicts.append(verdict)
        verdicts.sort(key=lambda v: -v["score"])
        return verdicts[:top_n]

    # ---------- the loop ----------

    async def run_forever(self, publish) -> None:
        """`publish` is an async callable(list[event]) that renders events
        into Telegram. Individual tick failures are logged and survived —
        the loop only exits on cancellation."""
        logger.info(
            "scanner loop starting (interval %ss, dry_run=%s)",
            config.SCAN_INTERVAL_SECONDS, self.engine.dry_run,
        )
        while True:
            try:
                events = await self.tick()
                if events:
                    await publish(events)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scanner tick crashed; continuing")
            await asyncio.sleep(config.SCAN_INTERVAL_SECONDS)
