"""
Token safety screen — the checks DexScreener market data can't give you:

- Mint authority ("contract renounced"): if it still exists, the deployer
  can print unlimited new supply into your position. Renounced (None) is
  the only good answer.
- Freeze authority: if it still exists, the deployer can freeze your token
  account — you buy, then can't sell. A common honeypot mechanic.
- Holder concentration: top-10 accounts' share of supply, EXCLUDING the
  single largest account (on a young pair that's almost always the
  liquidity pool itself). High concentration = a few wallets can exit on
  your head.
- LP lock: percentage of the pool's LP tokens locked or burned (via
  RugCheck, see clients/rugcheck.py). Unlocked LP means the deployer can
  pull the liquidity — the classic rug.

Caveat honestly: these reduce scam exposure, they don't eliminate it.
Deployers split stashes across fresh wallets, lock LP for a week and rug
on expiry, and none of this sees off-chain coordination. `security_strict`
(settings) decides what happens when a check can't complete: strict
rejects unknowns, lenient passes them with the ❓ displayed.
"""
import asyncio
import time
from dataclasses import dataclass

from .. import config, constants


@dataclass
class SafetyReport:
    mint: str
    decimals: int = None
    mint_renounced: bool = None    # None = unknown (RPC unavailable/failed)
    freeze_none: bool = None
    top10_pct: float = None        # excludes the largest single account
    lp_locked_pct: float = None    # None = no LP source could tell us
    lp_source: str = None          # which service answered ("rugcheck"/"goplus")
    standard_token: bool = None    # classic SPL Token program? False = Token-2022 etc.
    error: str = None

    def _checks(self) -> list:
        checks = [
            self.mint_renounced,
            self.freeze_none,
            self.standard_token,
            (self.top10_pct <= config.MAX_TOP10_HOLDER_PCT)
            if self.top10_pct is not None else None,
        ]
        if config.LP_CHECK_ENABLED:
            checks.append(
                (self.lp_locked_pct >= config.MIN_LP_LOCKED_PCT)
                if self.lp_locked_pct is not None else None
            )
        return checks

    def passes(self, strict: bool) -> bool:
        checks = self._checks()
        if strict:
            return all(c is True for c in checks)
        return all(c is not False for c in checks)  # unknowns pass, known-bad rejects

    def line(self) -> str:
        def mark(value, good, bad):
            if value is None:
                return "❓"
            return good if value else bad
        top10 = f"{self.top10_pct:.0f}%" if self.top10_pct is not None else "❓"
        parts = [
            f"Mint {mark(self.mint_renounced, '✅ renounced', '🚨 ACTIVE')}",
            f"Freeze {mark(self.freeze_none, '✅ none', '🚨 ACTIVE')}",
            f"Top10 {top10}",
        ]
        if self.standard_token is False:
            parts.append("🚨 Token-2022 (sell-trap extensions possible)")
        if config.LP_CHECK_ENABLED:
            source = {"goplus": " ·GP", "curve": " ·curve",
                      "pumpfun": " ·pf"}.get(self.lp_source, "")
            if self.lp_locked_pct is None:
                parts.append("LP ❓")
            elif self.lp_locked_pct >= config.MIN_LP_LOCKED_PCT:
                parts.append(f"LP 🔒 {self.lp_locked_pct:.0f}%{source}")
            else:
                parts.append(f"LP 🚨 only {self.lp_locked_pct:.0f}% locked{source}")
        return " | ".join(parts)


def structural_lp_lock(pair):
    """(pct, source) when the trading venue itself makes an LP pull
    impossible, or None. Used strictly as the LAST rung of the LP chain —
    only consulted when RugCheck and GoPlus both answered unknown, and
    never allowed to override real evidence from either.

    - pump.fun / Raydium LaunchLab bonding curves ("pumpfun"/"launchlab"
      dexIds): there are no LP tokens AT ALL — liquidity sits in program
      escrow until graduation. Nothing exists to pull.
    - PumpSwap pools for pump.fun-minted tokens (mint ends in "pump"):
      created by pump.fun's graduation migration, which locks the
      liquidity in the protocol permanently.

    Deliberately NOT inferred: plain Raydium pools. Anyone can open a
    Raydium pool and keep the LP tokens — that's the classic rug — and a
    scammer can vanity-grind a mint ending in "pump" and list it straight
    on Raydium to spoof exactly this kind of inference. Raydium pools must
    prove their lock through RugCheck/GoPlus like everyone else."""
    if not isinstance(pair, dict):
        return None
    dex_id = str(pair.get("dexId") or "").strip().lower()
    if dex_id in ("pumpfun", "launchlab"):
        return 100.0, "curve"
    mint = str((pair.get("baseToken") or {}).get("address") or "")
    if dex_id == "pumpswap" and mint.endswith("pump"):
        return 100.0, "pumpfun"
    return None


def risk_tier(report) -> str:
    """Three-way risk classification, independent of the user's strict
    setting: 'safe' = every check proven good; 'risky' = at least one
    check known-bad; 'unverified' = nothing known-bad but not all proven
    (or no report at all)."""
    if report is None:
        return "unverified"
    if report.passes(strict=True):
        return "safe"
    if not report.passes(strict=False):
        return "risky"
    return "unverified"


class SafetyChecker:
    # Seconds between uncached checks. Each check is 2 RPC calls; free/public
    # RPCs rate-limit hard, and a 429 shows up to the user as ❓ unverified —
    # spacing the calls keeps the data flowing on modest infrastructure.
    MIN_CHECK_INTERVAL = 0.4

    def __init__(self, rpc, rugcheck=None, cache_ttl: int = None, goplus=None):
        self._rpc = rpc
        # LP-lock sources, tried in order until one answers. RugCheck is
        # primary; GoPlus is the independent backup so one service's
        # outage or index gap doesn't leave coins stuck at ❓ unverified.
        self._lp_sources = [
            (name, source)
            for name, source in (("rugcheck", rugcheck), ("goplus", goplus))
            if source is not None
        ]
        self._goplus = goplus
        self._ttl = cache_ttl or config.SAFETY_CACHE_TTL_SECONDS
        self._cache = {}  # mint -> (SafetyReport, fetched_at, ttl)
        self._last_check_at = 0.0
        # One instance is shared by the scanner task and Telegram handlers;
        # the lock serializes uncached checks so concurrent callers can't
        # duplicate fetches or leapfrog the pacing sleep.
        self._lock = asyncio.Lock()

    def cached(self, mint: str):
        """The fresh cached report for a mint, or None — lets callers see
        whether a check() would be instant or would hit the network."""
        cached = self._cache.get(mint)
        if cached and time.time() - cached[1] < cached[2]:
            return cached[0]
        return None

    async def check(self, mint: str, pair: dict = None) -> SafetyReport:
        """`pair` (the DexScreener pair being evaluated, optional) enables
        the structural-lock fallback for venues where an LP pull is
        impossible by construction — see structural_lp_lock()."""
        cached = self._cache.get(mint)
        if cached and time.time() - cached[1] < cached[2]:
            return cached[0]

        async with self._lock:
            cached = self._cache.get(mint)  # a concurrent caller may have filled it
            if cached and time.time() - cached[1] < cached[2]:
                return cached[0]
            return await self._check_uncached(mint, pair)

    async def _check_uncached(self, mint: str, pair: dict = None) -> SafetyReport:
        wait = self.MIN_CHECK_INTERVAL - (time.time() - self._last_check_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_check_at = time.time()

        report = SafetyReport(mint=mint)
        try:
            mint_info = await self._rpc.get_mint_info(mint)
            if mint_info is None:
                report.error = "mint account not found/parseable"
            else:
                report.decimals = mint_info["decimals"]
                report.mint_renounced = mint_info["mint_authority"] is None
                report.freeze_none = mint_info["freeze_authority"] is None
                owner = mint_info.get("owner_program")
                report.standard_token = (
                    None if owner is None else owner == constants.TOKEN_PROGRAM_ID
                )
                supply = mint_info["supply"]
                if supply > 0:
                    largest = await self._rpc.get_token_largest_accounts(mint)
                    amounts = sorted(
                        (int(a.get("amount") or 0) for a in largest), reverse=True
                    )
                    # Drop the biggest account (presumed LP), take the next 10.
                    top10 = sum(amounts[1:11])
                    report.top10_pct = 100.0 * top10 / supply
        except Exception as exc:  # RPC down/rate-limited — report unknown, don't crash the scan
            report.error = f"{type(exc).__name__}: {exc}"

        if config.LP_CHECK_ENABLED:
            for source_name, source in self._lp_sources:
                try:
                    pct = await source.lp_locked_pct(mint)
                except Exception:
                    pct = None  # unreachable -> try the next source
                if pct is not None:
                    report.lp_locked_pct = pct
                    report.lp_source = source_name
                    break
            if report.lp_locked_pct is None:
                # Both real sources came up unknown (young pools often
                # aren't indexed yet). Venue structure is the last word —
                # a bonding curve has no LP to pull, a pump.fun migration
                # locks it — but real evidence above always wins.
                structural = structural_lp_lock(pair)
                if structural is not None:
                    report.lp_locked_pct, report.lp_source = structural

        # If the direct RPC reads failed, GoPlus can still answer the
        # authority questions — a second opinion beats an unknown.
        if self._goplus is not None and (
            report.mint_renounced is None or report.freeze_none is None
        ):
            try:
                authorities = await self._goplus.authorities(mint)
            except Exception:
                authorities = None
            if authorities:
                if report.mint_renounced is None:
                    report.mint_renounced = authorities.get("mint_renounced")
                if report.freeze_none is None:
                    report.freeze_none = authorities.get("freeze_none")

        # Unknowns get a short TTL so transient API failures retry soon
        # instead of poisoning a token's verdict for 10 minutes.
        incomplete = report.error is not None or (
            config.LP_CHECK_ENABLED and self._lp_sources
            and report.lp_locked_pct is None
        )
        ttl = 120 if incomplete else self._ttl
        self._cache[mint] = (report, time.time(), ttl)
        return report
