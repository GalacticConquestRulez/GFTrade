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
    Returns a float 0-100, or None when the report doesn't say."""
    if not isinstance(data, dict):
        return None
    best = None
    for market in data.get("markets") or []:
        if not isinstance(market, dict):
            continue
        lp = market.get("lp") or {}
        pct = lp.get("lpLockedPct")
        if isinstance(pct, (int, float)):
            best = max(best if best is not None else 0.0, float(pct))
    if best is not None:
        return best
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
