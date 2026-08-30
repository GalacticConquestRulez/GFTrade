"""
GoPlus Security client — the BACKUP LP-lock (and authority) source,
consulted only when RugCheck can't answer a mint.

Endpoint (verified against GoPlus's own swagger-generated SDK, v1.2.7):
  GET {base}/solana/token_security?contract_addresses={mint}
  -> {"code": 1, "message": "ok", "result": {"<mint>": {...}}}
One mint per request (the Solana endpoint does not batch), keyless,
free tier ~30 calls/min — fine for a fallback role.

Evidence rules (deliberately asymmetric): this backup can PROVE a lock,
but never proves an unlock —
  - dex[]: burn_percent (0-100) of the main pool counts only when > 0.
    A burn_percent of 0 means "not burned", not "not locked" (locker
    contracts aren't burns), so zero yields no verdict.
  - Main pool = highest parseable TVL. With several pools and no usable
    TVLs the main pool is unidentifiable -> no verdict (a burned dust
    pool must not vouch for the token). A single pool needs no tie-break.
  - lp_holders[]: positively locked holders (tag "Burn" or is_locked=1)
    sum into a lock percentage; absence of the optional lock flags is
    never read as "unlocked". percent is a STRING FRACTION 0-1 — values
    outside [0, 1] are rejected as scale drift, not rescaled.
Net effect: GoPlus can rescue a coin from ❓ to ✅, but a 🚫 banishment
always requires primary-source or on-chain evidence.

Authorities: mintable/freezable objects {status, authority[]}; status
"1"/1 = authority ACTIVE (NOT renounced), "0"/0 = renounced.
"""
import asyncio
import time

from .. import config


def _main_pool_burn_pct(token_data: dict):
    pools = [p for p in (token_data.get("dex") or []) if isinstance(p, dict)]
    if not pools:
        return None

    def burn_of(pool):
        try:
            burn = float(pool.get("burn_percent"))
        except (TypeError, ValueError):
            return None
        return max(0.0, min(burn, 100.0))

    if len(pools) == 1:
        burn = burn_of(pools[0])
    else:
        sized = []
        for pool in pools:
            try:
                tvl = float(pool.get("tvl"))
            except (TypeError, ValueError):
                continue
            if tvl > 0:
                sized.append((tvl, pool))
        if not sized:
            return None  # can't identify the main pool -> no verdict
        burn = burn_of(max(sized, key=lambda item: item[0])[1])
    if burn is None or burn <= 0:
        return None  # "not burned" is not evidence of "not locked"
    return burn


def _locked_holders_pct(token_data: dict):
    total = None
    for holder in token_data.get("lp_holders") or []:
        if not isinstance(holder, dict):
            continue
        try:
            fraction = float(holder.get("percent"))
        except (TypeError, ValueError):
            continue
        if not 0 <= fraction <= 1:
            continue  # scale drift or data bug — reject, don't rescale
        locked = (holder.get("is_locked") in (1, "1", True)
                  or str(holder.get("tag") or "").strip().lower() == "burn")
        if locked and fraction > 0:
            total = (total or 0.0) + fraction * 100
    if total is None:
        return None
    return min(total, 100.0)


def parse_lp_locked_pct(token_data: dict):
    """LP locked/burned percentage from a GoPlus Solana token_security
    entry, or None when there is no positive lock evidence."""
    if not isinstance(token_data, dict):
        return None
    signals = [s for s in (_main_pool_burn_pct(token_data),
                           _locked_holders_pct(token_data)) if s is not None]
    return max(signals) if signals else None


def parse_authorities(token_data: dict):
    """{'mint_renounced': bool|None, 'freeze_none': bool|None} from the
    mintable/freezable objects; None overall when neither is readable.
    GoPlus status semantics: 1/'1' = authority ACTIVE, 0/'0' = renounced."""
    if not isinstance(token_data, dict):
        return None

    def renounced(obj):
        status = (obj or {}).get("status") if isinstance(obj, dict) else None
        if status in ("0", "1", 0, 1):
            return str(status) == "0"
        return None

    result = {
        "mint_renounced": renounced(token_data.get("mintable")),
        "freeze_none": renounced(token_data.get("freezable")),
    }
    if result["mint_renounced"] is None and result["freeze_none"] is None:
        return None
    return result


class GoPlus:
    def __init__(self, client, api_base: str = None):
        self._client = client
        self.api_base = api_base or config.GOPLUS_API_BASE
        self._last_call_at = 0.0
        self._cache = {}  # mint -> (token_data|None, fetched_at)
        # Staggers request starts under concurrency; requests overlap.
        self._pace = asyncio.Lock()

    async def _security(self, mint: str):
        """Fetch (or serve cached) token_security data. Never raises: any
        failure — network, non-JSON body, unexpected envelope — caches and
        returns None, so a broken backup can neither crash the safety
        chain nor dodge its own pacing by skipping the cache."""
        cached = self._cache.get(mint)
        if cached is not None:
            data, fetched_at = cached
            ttl = 300 if data is not None else 120
            if time.time() - fetched_at < ttl:
                return data

        # ~30 req/min free tier; this is a fallback source, so pace politely.
        async with self._pace:
            wait = 2.1 - (time.time() - self._last_call_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call_at = time.time()

        data = None
        try:
            resp = await self._client.get(
                f"{self.api_base}/solana/token_security",
                params={"contract_addresses": mint},
                timeout=6,
            )
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict) and body.get("code") in (1, "1"):
                    result = body.get("result")
                    if isinstance(result, dict):
                        entry = result.get(mint)
                        if isinstance(entry, dict):
                            data = entry
        except Exception:
            data = None
        self._cache[mint] = (data, time.time())
        return data

    async def lp_locked_pct(self, mint: str):
        data = await self._security(mint)
        return parse_lp_locked_pct(data) if data else None

    async def authorities(self, mint: str):
        data = await self._security(mint)
        return parse_authorities(data) if data else None
