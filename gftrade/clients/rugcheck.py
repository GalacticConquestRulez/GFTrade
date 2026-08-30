"""
Minimal RugCheck client — used for exactly one question the chain can't
answer cheaply: what percentage of this token's liquidity is locked or
burned? (Reading that from raw RPC means parsing every DEX's pool account
layout; RugCheck already does it.)

Public API, no key: GET {base}/tokens/{mint}/report
Parsing is deliberately defensive — the schema has evolved — and anything
unparseable returns None (= unknown) rather than a guess. The caller
decides what unknown means (strict mode: reject).
"""
import asyncio
import time

from .. import config


def parse_lp_locked_pct(data: dict):
    """Extract the LP locked percentage from a RugCheck report.
    Returns a float 0-100, or None when the report doesn't say.

    Multi-market rule: when a token trades in several pools, taking the
    max would let one tiny 100%-burned dust pool vouch for a token whose
    real liquidity is unlocked elsewhere (and taking the min would banish
    tokens over irrelevant side pools). RugCheck reports don't carry a
    reliable pool-size field to weight by, so: markets that broadly agree
    (spread <= 20 points) yield the max; markets that conflict yield None
    — conflicting evidence is unknown, and the safety chain then asks the
    backup source, which selects the main pool by TVL."""
    if not isinstance(data, dict):
        return None
    pcts = []
    for market in data.get("markets") or []:
        if not isinstance(market, dict):
            continue
        pct = (market.get("lp") or {}).get("lpLockedPct")
        if isinstance(pct, (int, float)) and 0 <= float(pct) <= 100:
            pcts.append(float(pct))
    if pcts:
        if max(pcts) - min(pcts) <= 20:
            return max(pcts)
        return None  # markets disagree -> let the backup source decide
    # Fallback: the risks list names an unlocked LP explicitly.
    for risk in data.get("risks") or []:
        name = str((risk or {}).get("name", "")).lower()
        if "lp" in name and "unlock" in name:
            return 0.0
    return None


class RugCheck:
    def __init__(self, client, api_base: str = None):
        self._client = client
        self.api_base = api_base or config.RUGCHECK_API_BASE
        self._last_call_at = 0.0

    async def lp_locked_pct(self, mint: str):
        """LP locked % for a mint, or None if RugCheck can't tell us.
        Politeness throttle: the free tier is tightly rate-limited."""
        wait = 1.2 - (time.time() - self._last_call_at)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call_at = time.time()
        resp = await self._client.get(
            f"{self.api_base}/tokens/{mint}/report", timeout=12
        )
        if resp.status_code != 200:
            return None
        return parse_lp_locked_pct(resp.json())
