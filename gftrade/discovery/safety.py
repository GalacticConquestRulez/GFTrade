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
import time
from dataclasses import dataclass

from .. import config


@dataclass
class SafetyReport:
    mint: str
    decimals: int = None
    mint_renounced: bool = None    # None = unknown (RPC unavailable/failed)
    freeze_none: bool = None
    top10_pct: float = None        # excludes the largest single account
    lp_locked_pct: float = None    # None = RugCheck couldn't tell us
    error: str = None

    def _checks(self) -> list:
        checks = [
            self.mint_renounced,
            self.freeze_none,
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
        if config.LP_CHECK_ENABLED:
            if self.lp_locked_pct is None:
                parts.append("LP ❓")
            elif self.lp_locked_pct >= config.MIN_LP_LOCKED_PCT:
                parts.append(f"LP 🔒 {self.lp_locked_pct:.0f}%")
            else:
                parts.append(f"LP 🚨 only {self.lp_locked_pct:.0f}% locked")
        return " | ".join(parts)


class SafetyChecker:
    def __init__(self, rpc, rugcheck=None, cache_ttl: int = None):
        self._rpc = rpc
        self._rugcheck = rugcheck
        self._ttl = cache_ttl or config.SAFETY_CACHE_TTL_SECONDS
        self._cache = {}  # mint -> (SafetyReport, fetched_at, ttl)

    async def check(self, mint: str) -> SafetyReport:
        cached = self._cache.get(mint)
        if cached and time.time() - cached[1] < cached[2]:
            return cached[0]

        report = SafetyReport(mint=mint)
        try:
            mint_info = await self._rpc.get_mint_info(mint)
            if mint_info is None:
                report.error = "mint account not found/parseable"
            else:
                report.decimals = mint_info["decimals"]
                report.mint_renounced = mint_info["mint_authority"] is None
                report.freeze_none = mint_info["freeze_authority"] is None
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

        if config.LP_CHECK_ENABLED and self._rugcheck is not None:
            try:
                report.lp_locked_pct = await self._rugcheck.lp_locked_pct(mint)
            except Exception:
                report.lp_locked_pct = None  # unreachable -> unknown

        # Unknowns get a short TTL so transient API failures retry soon
        # instead of poisoning a token's verdict for 10 minutes.
        incomplete = report.error is not None or (
            config.LP_CHECK_ENABLED and self._rugcheck is not None
            and report.lp_locked_pct is None
        )
        ttl = 120 if incomplete else self._ttl
        self._cache[mint] = (report, time.time(), ttl)
        return report
