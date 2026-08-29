"""
GeckoTerminal public API client — used for one thing: the newest Solana
pools. This is the "catch coins earlier" feed: every new pool (fresh
launches and pump.fun graduations alike) shows up here within minutes of
creation, whereas DexScreener's token-profiles feed only lists tokens
whose creators made a profile, often hours later.

Free, keyless, ~30 req/min limit. Docs: https://apiguide.geckoterminal.com
One call per scan tick keeps us far under the limit. Parsing is defensive:
a changed schema degrades to an empty list (and the profile feed still
drives discovery), never a crash.
"""
import httpx

BASE = "https://api.geckoterminal.com/api/v2"


def parse_new_pool_mints(data: dict) -> list:
    """Extract base-token mints from a /networks/solana/new_pools response.
    Token ids look like 'solana_<mint>'."""
    mints, seen = [], set()
    if not isinstance(data, dict):
        return mints
    for pool in data.get("data") or []:
        if not isinstance(pool, dict):
            continue
        token = (((pool.get("relationships") or {}).get("base_token") or {})
                 .get("data") or {})
        token_id = token.get("id") or ""
        if "_" not in token_id:
            continue
        mint = token_id.split("_", 1)[1]
        if 32 <= len(mint) <= 44 and mint not in seen:
            seen.add(mint)
            mints.append(mint)
    return mints


def parse_simple_prices(data: dict) -> dict:
    """Extract {mint: price} from a /simple/.../token_price response."""
    prices = {}
    if not isinstance(data, dict):
        return prices
    attrs = ((data.get("data") or {}).get("attributes") or {})
    for mint, raw in (attrs.get("token_prices") or {}).items():
        try:
            price = float(raw)
        except (TypeError, ValueError):
            continue
        if price > 0:
            prices[mint] = price
    return prices


class GeckoTerminal:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def simple_token_prices(self, mints: list) -> dict:
        """Batch USD prices (up to 30 mints per call) — the second price
        source: cheap checkpoint fills, and the failover that keeps exit
        checks alive when DexScreener rate-limits."""
        prices = {}
        for i in range(0, len(mints), 30):
            batch = mints[i:i + 30]
            resp = await self._client.get(
                f"{BASE}/simple/networks/solana/token_price/{','.join(batch)}",
                headers={"accept": "application/json"},
                timeout=15,
            )
            if resp.status_code != 200:
                continue
            prices.update(parse_simple_prices(resp.json()))
        return prices

    async def new_solana_pool_mints(self) -> list:
        resp = await self._client.get(
            f"{BASE}/networks/solana/new_pools",
            params={"page": 1},
            headers={"accept": "application/json"},
            timeout=15,
        )
        if resp.status_code != 200:
            return []
        return parse_new_pool_mints(resp.json())
