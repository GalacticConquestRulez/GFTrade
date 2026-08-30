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
from .discovery import safety as safety_mod
from .discovery.trend import PriceHistory

logger = logging.getLogger(__name__)

POOL_NO_PAIR_GRACE_HOURS = 3   # drop pool entries that never produce a tradable pair
EVAL_BATCH_PER_TICK = 120      # pool mints re-checked per tick (4 DexScreener calls)

# Cap on UNCACHED safety checks per pass. Each one can cost seconds of
# paced network calls (worse when a source is timing out), and an
# unbounded pass once crawled for minutes — coins past the cap simply
# stay ❓ this pass and get checked on the next; the cache makes coverage
# catch up quickly. Cached checks are always free and never counted.
MAX_UNCACHED_SAFETY_PER_PASS = 8
SCAN_NOW_SAFETY_BUDGET = 12


class Scanner:
    def __init__(self, store, dex, engine, safety_checker, gecko=None,
                 factors=None, prices=None):
        self.store = store
        self.dex = dex
        self.engine = engine
        self.safety = safety_checker
        self.gecko = gecko  # GeckoTerminal new-pools feed (optional)
        self.factors = factors  # FactorLog (optional)
        self.prices = prices or PriceHistory()
        # mint -> first_seen ts. In-memory only: after a restart the feeds
        # repopulate it within a few ticks.
        self.pool = {}
        self.last_tick_at = None
        self.last_tick_stats = {}
        # When the currently-running discovery sweep started, so /start can
        # say "first sweep running (40s in)" instead of a scary "never".
        self.sweep_started_at = None
        # feed name -> "ok" / "error" from the most recent attempt, so
        # /start can show whether discovery data is actually flowing
        self.feed_status = {}
        # /scan results cached for pagination:
        # {"verdicts": [...], "at": ts, "evaluated": how many pool mints had pairs}
        self.last_scan = None

    # ---------- pool management ----------

    def _absorb_mints(self, mints: list) -> int:
        added = 0
        for mint in mints:
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

    def _absorb_profiles(self, profiles: list) -> int:
        return self._absorb_mints([
            p.get("tokenAddress") for p in profiles
            if p.get("chainId") == constants.CHAIN_ID
        ])

    # ---------- evaluation ----------

    async def evaluate_pair(self, pair: dict, boosted: set,
                            safety_budget: list = None) -> dict:
        """Full verdict for one pair: screen -> safety -> patterns -> score.

        `safety_budget` is a single-element mutable counter of UNCACHED
        safety checks this pass may still spend. When it's exhausted, the
        coin is evaluated with safety unknown (❓ this pass, retried next
        pass) instead of stalling the pass on paced network calls. None
        (the default, used by the on-demand token card) means unlimited."""
        ok, reasons = filters.screen_pair(pair, boosted,
                                          overrides=self.store.settings)
        verdict = {
            "pair": pair,
            "mint": (pair.get("baseToken") or {}).get("address"),
            "screened_ok": ok,
            "reject_reasons": reasons,
            "safety": None,
            "safety_ok": False,   # all safety checks proven good (strict-aware)
            "patterns": [],
            "score": 0,
            "breakdown": {},
        }
        verdict["extension_pct"] = self.prices.extension_pct(verdict["mint"])
        verdict["market_score"] = scoring.market_score_pair(pair)
        verdict["risk_tier"] = "unverified"
        if not ok:
            return verdict
        strict = self.store.settings["security_strict"]
        verdict["patterns"] = patterns.scan(pair)

        if safety_budget is not None and self.safety.cached(verdict["mint"]) is None:
            if safety_budget[0] <= 0:
                # Budget spent: stay ❓ this pass rather than stalling it.
                verdict["score"], verdict["breakdown"] = scoring.score_pair(
                    pair, None, strict
                )
                return verdict
            safety_budget[0] -= 1

        verdict["safety"] = await self.safety.check(verdict["mint"], pair=pair)
        verdict["safety_ok"] = verdict["safety"].passes(strict)
        verdict["risk_tier"] = safety_mod.risk_tier(verdict["safety"])
        verdict["score"], verdict["breakdown"] = scoring.score_pair(
            pair, verdict["safety"], strict
        )
        return verdict

    def _too_extended(self, verdict: dict) -> bool:
        """Trend-stage gate: price already far above its own recent low is
        a late entry — the setup that buys a pump's top. Unknown extension
        (thin history) passes: the min-age screen already provides some
        observation runway, and refusing to act on missing data here would
        silence the scanner after every restart."""
        limit = self.store.settings.get("max_entry_extension_pct") or 0
        extension = verdict.get("extension_pct")
        return bool(limit) and extension is not None and extension > limit

    def _qualifies(self, verdict: dict) -> bool:
        """Alert gate. Fully-safe (✅) coins always qualify on pattern+score.
        With alert_unverified on, ❓-only coins (no check known-bad, some
        unverifiable) qualify too — for manual flips. Known-bad coins never
        alert. Autobuy applies its own stricter ✅-only check on top."""
        settings = self.store.settings
        safety = verdict["safety"]
        safety_acceptable = verdict["safety_ok"] or (
            settings.get("alert_unverified")
            and safety is not None
            and safety.passes(strict=False)  # lenient = nothing known-bad
        )
        return (
            verdict["screened_ok"]
            and safety_acceptable
            and verdict["patterns"]
            and verdict["score"] >= settings["min_alert_score"]
            and not self._too_extended(verdict)
        )

    # ---------- the tick ----------

    async def tick(self, discover: bool = True, exits: bool = True) -> list:
        """One pass. run_forever calls this with exits-only on the fast
        cadence, and discovery runs as its own task (exits=False) so a slow
        discovery pass never delays position protection. The default
        (both on) is the one-shot behavior tests and callers rely on."""
        events = []
        stats = {"profiles_new": 0, "pool": 0, "checked": 0, "passed_screen": 0,
                 "signals": 0}

        if exits:
            # Manage what we already hold — exits take priority over entries.
            try:
                events.extend(await self.engine.check_exits())
            except Exception as exc:
                logger.exception("exit check failed")
                events.append({"type": "scan_error", "where": "check_exits",
                               "error": str(exc)})

        if not discover:
            return events

        self.sweep_started_at = time.time()
        settings = self.store.settings
        if settings["scanner_on"]:
            try:
                events.extend(await self._discover(stats))
            except Exception as exc:
                logger.exception("discovery failed")
                events.append({"type": "scan_error", "where": "discovery", "error": str(exc)})

        # Fill in signal-outcome checkpoints for the report card (silent).
        try:
            await self._update_signal_log()
        except Exception:
            logger.exception("signal log update failed")
        try:
            await self._update_factor_checkpoints()
        except Exception:
            logger.exception("factor checkpoint update failed")

        stats["pool"] = len(self.pool)
        self.last_tick_at = time.time()
        self.last_tick_stats = stats
        return events

    async def _discover(self, stats: dict) -> list:
        events = []
        settings = self.store.settings

        try:
            profiles = await self.dex.token_profiles_latest()
            stats["profiles_new"] = self._absorb_profiles(profiles)
            self.feed_status["profiles"] = "ok"
        except Exception:
            logger.warning("token-profiles feed unavailable this tick")
            self.feed_status["profiles"] = "error"
        if self.gecko is not None:
            # The early feed: every new Solana pool (pump.fun graduations
            # included) minutes after creation, not just profiled tokens.
            try:
                mints = await self.gecko.new_solana_pool_mints()
                stats["pools_new"] = self._absorb_mints(mints)
                # An empty parse every time would mean the schema changed on
                # us — surface that as degraded rather than silently "ok".
                self.feed_status["new-pools"] = "ok" if mints else "empty"
            except Exception:
                logger.warning("geckoterminal new-pools feed unavailable this tick")
                self.feed_status["new-pools"] = "error"

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
        safety_budget = [MAX_UNCACHED_SAFETY_PER_PASS]
        pairs = await self.dex.pairs_for_tokens(constants.CHAIN_ID, batch)

        best_by_mint = {}
        for pair in pairs:
            mint = (pair.get("baseToken") or {}).get("address")
            if mint in self.pool:
                current = best_by_mint.get(mint)
                liq = (pair.get("liquidity") or {}).get("usd") or 0
                if current is None or liq > ((current.get("liquidity") or {}).get("usd") or 0):
                    best_by_mint[mint] = pair

        # Feed the rolling price history from everything we just fetched —
        # this is the data the extension gate and vol-scaled exits run on.
        for mint, pair in best_by_mint.items():
            self.prices.record(mint, float(pair.get("priceUsd") or 0))
        self.prices.prune()

        now = time.time()
        for mint in batch:
            pair = best_by_mint.get(mint)
            if pair is None:
                if now - self.pool[mint] > POOL_NO_PAIR_GRACE_HOURS * 3600:
                    del self.pool[mint]
                continue
            age_hours = filters.pair_age_hours(pair)
            if age_hours > settings.get("max_pair_age_hours", config.MAX_PAIR_AGE_HOURS):
                del self.pool[mint]  # aged out of our window for good
                continue

            stats["checked"] += 1
            verdict = await self.evaluate_pair(pair, boosted,
                                               safety_budget=safety_budget)
            if self.factors is not None:
                try:  # every evaluated candidate is logged, passed or failed
                    self.factors.log_snapshot(verdict)
                except Exception:
                    logger.exception("factor logging failed")
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
            self._record_signal(verdict)

            can_autobuy = (
                settings["autobuy"]
                and verdict["safety_ok"]  # autobuy never touches ❓ coins
                and verdict["score"] >= settings["min_autobuy_score"]
                and age_hours * 60 >= (settings.get("autobuy_min_age_minutes") or 0)
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

    # ---------- signal report card ----------

    # How long after a signal each price checkpoint is due, and how long
    # past due we keep looking for a price before declaring the token dead
    # (no tradable pair left) and recording a total loss.
    SIGNAL_HORIZONS = {"h1": 3600, "h6": 6 * 3600, "h24": 24 * 3600}
    SIGNAL_DEAD_GRACE = 2 * 3600

    def _record_signal(self, verdict: dict) -> None:
        pair = verdict["pair"]
        price0 = float(pair.get("priceUsd") or 0)
        if price0 <= 0:
            return
        self.store.add_signal({
            "mint": verdict["mint"],
            "symbol": (pair.get("baseToken") or {}).get("symbol") or "?",
            "pattern": verdict["patterns"][0]["pattern"] if verdict["patterns"] else "none",
            "score": verdict["score"],
            "price0": price0,
            "ts": time.time(),
            "h1": None, "h6": None, "h24": None,
        })

    async def _fetch_prices(self, mints: list) -> dict:
        """Price-only lookups for checkpoints: GeckoTerminal first (spares
        DexScreener's budget for screening and exits), DexScreener for
        whatever GT doesn't cover. A mint only counts as dead when BOTH
        sources fail to price it past the grace window."""
        prices = {}
        if self.gecko is not None:
            try:
                prices.update(await self.gecko.simple_token_prices(mints))
            except Exception:
                logger.warning("gecko price batch failed; using dexscreener only")
        missing = [m for m in mints if m not in prices]
        if missing:
            try:
                pairs = await self.dex.pairs_for_tokens(constants.CHAIN_ID, missing)
            except Exception:
                logger.warning("dexscreener price batch failed; partial prices")
                return prices
            best = {}
            for pair in pairs:
                mint = (pair.get("baseToken") or {}).get("address")
                liq = (pair.get("liquidity") or {}).get("usd") or 0
                current = best.get(mint)
                if current is None or liq > current[1]:
                    best[mint] = (float(pair.get("priceUsd") or 0), liq)
            prices.update({m: p for m, (p, _) in best.items() if p > 0})
        return prices

    async def _update_signal_log(self) -> None:
        """Fill due price checkpoints so /trades can show how signals
        actually performed. A token with no price left after the grace
        period is recorded at price 0 — a rug is a result, not a gap."""
        now = time.time()
        due_mints = set()
        for entry in self.store.signal_log:
            for key, delta in self.SIGNAL_HORIZONS.items():
                if entry.get(key) is None and now >= entry["ts"] + delta:
                    due_mints.add(entry["mint"])
        if not due_mints:
            return
        prices = await self._fetch_prices(list(due_mints)[:30])

        changed = False
        for entry in self.store.signal_log:
            for key, delta in self.SIGNAL_HORIZONS.items():
                if entry.get(key) is not None or now < entry["ts"] + delta:
                    continue
                price = prices.get(entry["mint"])
                if price is not None and price > 0:
                    entry[key] = price
                    changed = True
                elif now >= entry["ts"] + delta + self.SIGNAL_DEAD_GRACE:
                    entry[key] = 0.0  # no market left = -100%
                    changed = True
        if changed:
            self.store.save()

    async def _update_factor_checkpoints(self) -> None:
        """One batched price fetch per discovery tick fills the factor
        log's due 1h/6h/24h outcome checkpoints."""
        if self.factors is None:
            return
        mints = self.factors.due_checkpoint_mints(limit=30)
        if not mints:
            return
        self.factors.fill_checkpoints(await self._fetch_prices(mints))

    # ---------- on-demand scan (/scan) ----------

    SCAN_MIN_LIST = 10  # /scan always tries to show at least this many, ranked

    async def scan_now(self, top_n: int = 30) -> list:
        """One synchronous sweep for the /scan command. Returns a ranked
        browsing list in three tiers, best score first within each:

          1. pass the market screens AND every safety check (✅)
          2. pass the market screens, fail/unverified on safety (🚫/❓)
          3. near-misses: fail the market screens, shown with the first
             reason why — included only when tiers 1+2 hold fewer than
             SCAN_MIN_LIST entries, so the list is never uselessly empty
             and the rejection reasons double as tuning feedback.

        Near-misses skip the on-chain safety lookups (no point spending
        rate-limited calls on coins that already failed) so their scores
        carry no safety points. Aged-out pairs never appear even as
        near-misses. Alerts and autobuy still only touch tier 1 — this
        list is for eyes, not money. Results are cached on self.last_scan
        for the paged Telegram view."""
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
        screened, near_misses = [], []
        evaluated = 0
        banned = 0  # known-risky coins removed from view entirely
        # Why candidates fell out of the list — rendered when the scan comes
        # up empty so "nothing to show" is a diagnosis, not a shrug.
        drops = {"no_pair": 0, "too_old": 0, "unvetted": 0}
        safety_budget = [SCAN_NOW_SAFETY_BUDGET]
        if batch:
            pairs = await self.dex.pairs_for_tokens(constants.CHAIN_ID, batch)
            best_by_mint = {}
            for pair in pairs:
                mint = (pair.get("baseToken") or {}).get("address")
                current = best_by_mint.get(mint)
                liq = (pair.get("liquidity") or {}).get("usd") or 0
                if current is None or liq > ((current.get("liquidity") or {}).get("usd") or 0):
                    best_by_mint[mint] = pair

            for mint, pair in best_by_mint.items():
                self.prices.record(mint, float(pair.get("priceUsd") or 0))

            strict = self.store.settings["security_strict"]
            evaluated = len(best_by_mint)
            drops["no_pair"] = len(batch) - evaluated
            for mint, pair in best_by_mint.items():
                verdict = await self.evaluate_pair(pair, boosted,
                                                   safety_budget=safety_budget)
                if verdict["screened_ok"]:
                    if verdict["risk_tier"] == "risky":
                        banned += 1  # unlocked LP / live authorities: never listed
                        continue
                    screened.append(verdict)
                    continue
                # Near-miss: keep unless it's simply too old for our window.
                if any("exceeds max" in r for r in verdict["reject_reasons"]):
                    drops["too_old"] += 1
                    continue
                verdict["patterns"] = patterns.scan(pair)
                verdict["score"], verdict["breakdown"] = scoring.score_pair(
                    pair, None, strict
                )
                near_misses.append(verdict)

        # Sort: risk tier first (safe -> unverified), then pure market
        # quality inside each tier. Known-risky coins were removed above —
        # a hot chart is not a reason to show a coin whose deployer can
        # pull the pool or print supply.
        tier_rank = {"safe": 0, "unverified": 1}
        screened.sort(key=lambda v: (tier_rank.get(v.get("risk_tier"), 1),
                                     -v.get("market_score", v["score"])))
        near_misses.sort(key=lambda v: -v.get("market_score", v["score"]))
        verdicts = screened[:top_n]
        if len(verdicts) < self.SCAN_MIN_LIST:
            # Near-misses skipped safety during screening (no point paying
            # for coins that failed market checks) — but nothing enters the
            # visible list without proving it isn't known-bad. Check lazily,
            # capped so a risky streak can't burn the API budget.
            checked = 0
            for index, verdict in enumerate(near_misses):
                if len(verdicts) >= self.SCAN_MIN_LIST:
                    break
                if checked >= self.SCAN_MIN_LIST * 2 or (
                    self.safety.cached(verdict["mint"]) is None
                    and safety_budget[0] <= 0
                ):
                    # can't verify more this sweep; unshown beats unvetted
                    drops["unvetted"] = len(near_misses) - index
                    break
                if self.safety.cached(verdict["mint"]) is None:
                    safety_budget[0] -= 1
                checked += 1
                try:
                    verdict["safety"] = await self.safety.check(
                        verdict["mint"], pair=verdict["pair"])
                except Exception:
                    verdict["safety"] = None
                verdict["risk_tier"] = safety_mod.risk_tier(verdict["safety"])
                if verdict["risk_tier"] == "risky":
                    banned += 1
                    continue
                verdicts.append(verdict)
        self.last_scan = {"verdicts": verdicts, "at": time.time(),
                          "evaluated": evaluated, "banned": banned,
                          "drops": drops}
        return verdicts

    # ---------- the loop ----------

    # A sweep must finish inside this or be cancelled and retried. Every
    # network call already has its own timeout, so this should never fire —
    # it's the last line of defense against an unforeseen wedge leaving
    # "last sweep never" on the board forever.
    SWEEP_TIMEOUT_SECONDS = 300

    async def _discovery_pass(self, publish) -> None:
        """One full discovery tick, run as its own task so a slow pass
        (safety-source outages, API backoffs) can never delay the exit
        checks that protect open positions."""
        try:
            events = await asyncio.wait_for(
                self.tick(discover=True, exits=False), self.SWEEP_TIMEOUT_SECONDS
            )
            if events:
                await publish(events)
        except asyncio.TimeoutError:
            logger.error("discovery sweep exceeded %ss; cancelled, will retry",
                         self.SWEEP_TIMEOUT_SECONDS)
            try:
                await publish([{
                    "type": "scan_error", "where": "discovery watchdog",
                    "error": (f"a sweep ran past {self.SWEEP_TIMEOUT_SECONDS}s and "
                              "was cancelled — safety sources are very slow right "
                              "now; retrying on the normal schedule"),
                }])
            except Exception:
                logger.exception("failed to publish watchdog event")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("discovery pass crashed; continuing")

    async def run_forever(self, publish) -> None:
        """`publish` is an async callable(list[event]) that renders events
        into Telegram. Exit checks run on the fast cadence in this loop;
        discovery runs every SCAN_INTERVAL_SECONDS as a separate task
        (never overlapping itself). Failures are logged and survived —
        the loop only exits on cancellation."""
        exit_interval = max(5, min(config.EXIT_CHECK_INTERVAL_SECONDS,
                                   config.SCAN_INTERVAL_SECONDS))
        logger.info(
            "scanner loop starting (discovery every %ss, exit checks every %ss, "
            "dry_run=%s)",
            config.SCAN_INTERVAL_SECONDS, exit_interval, self.engine.dry_run,
        )
        last_discovery = 0.0
        discovery_task = None
        try:
            while True:
                try:
                    events = await self.tick(discover=False)
                    if events:
                        await publish(events)
                    discovery_done = discovery_task is None or discovery_task.done()
                    if (discovery_done
                            and time.time() - last_discovery >= config.SCAN_INTERVAL_SECONDS):
                        last_discovery = time.time()
                        discovery_task = asyncio.create_task(
                            self._discovery_pass(publish)
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("scanner tick crashed; continuing")
                await asyncio.sleep(exit_interval)
        finally:
            if discovery_task is not None and not discovery_task.done():
                discovery_task.cancel()
