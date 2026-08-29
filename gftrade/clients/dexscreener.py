"""
Async client for DexScreener's public REST API (no key required).
Docs: https://docs.dexscreener.com/api/reference

Rate limits: 60/min on token-profile & boost endpoints, 300/min on
pair/token endpoints. The scanner's cadence (one profiles+boosts sweep and
a handful of batched token lookups per tick) stays far under both, but we
still back off once on a 429 to be a good citizen.
"""
import asyncio
import logging
import time

import httpx

from .. import constants

logger = logging.getLogger(__name__)

BASE = "https://api.dexscreener.com"
TOKEN_BATCH_SIZE = 30  # /tokens/v1 accepts up to 30 comma-separated addresses


class DexScreener:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._sol_price_cache = (0.0, 0.0)  # (price_usd, fetched_at)

    async def _get(self, path: str, params: dict = None):
        for attempt in (1, 2):
            resp = await self._client.get(f"{BASE}{path}", params=params, timeout=15)
            if resp.status_code == 429 and attempt == 1:
                retry_after = float(resp.headers.get("retry-after", 2) or 2)
                await asyncio.sleep(min(retry_after, 10))
                continue
            resp.raise_for_status()
            return resp.json()

    # ---------- discovery feeds ----------

    async def token_profiles_latest(self) -> list:
        """Recently created/updated token profiles — the closest keyless proxy
        for 'new token' DexScreener offers. Not every new pair has a profile
        and not every profile is a fresh pair; the age filter sorts that out."""
        return await self._get("/token-profiles/latest/v1") or []

    async def boosted_token_addresses(self) -> set:
        """(chainId, tokenAddress-lowercase) for tokens currently paying for
        promotion. Used to EXCLUDE, not include."""
        latest = await self._get("/token-boosts/latest/v1") or []
        top = await self._get("/token-boosts/top/v1") or []
        return {
            (b.get("chainId"), (b.get("tokenAddress") or "").lower())
            for b in latest + top
            if b.get("tokenAddress")
        }

    # ---------- pair lookups ----------

    async def pairs_for_tokens(self, chain_id: str, addresses: list) -> list:
        """All pairs for up to N token addresses, batched 30 per request."""
        pairs = []
        for i in range(0, len(addresses), TOKEN_BATCH_SIZE):
            batch = addresses[i:i + TOKEN_BATCH_SIZE]
            data = await self._get(f"/tokens/v1/{chain_id}/{','.join(batch)}") or []
            pairs.extend(p for p in data if p.get("chainId") == chain_id)
            if i + TOKEN_BATCH_SIZE < len(addresses):
                await asyncio.sleep(0.25)
        return pairs

    async def pairs_for_token(self, chain_id: str, address: str) -> list:
        return await self.pairs_for_tokens(chain_id, [address])

    async def search(self, query: str) -> list:
        data = await self._get("/latest/dex/search", params={"q": query})
        return (data or {}).get("pairs") or []

    @staticmethod
    def best_of(pairs: list):
        """The most liquid pair — the one that represents the token's real market."""
        if not pairs:
            return None
        return max(pairs, key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0))

    async def best_pair(self, chain_id: str, mint: str):
        return self.best_of(await self.pairs_for_token(chain_id, mint))

    # ---------- reference prices ----------

    async def sol_price_usd(self) -> float:
        """SOL/USD from the most liquid stable-quoted SOL pair, cached 60s.
        On a fetch failure the last known price is served however old it is
        (with a warning) — SOL barely moves on the timescale of an outage,
        and a stale conversion beats a dead exit check."""
        price, fetched_at = self._sol_price_cache
        if time.time() - fetched_at < 60 and price > 0:
            return price
        try:
            pairs = await self.pairs_for_token(constants.CHAIN_ID, constants.SOL_MINT)
        except Exception:
            if price > 0:
                logger.warning("SOL price fetch failed; serving cached value")
                return price
            raise
        stable_quoted = [
            p for p in pairs
            if (p.get("baseToken") or {}).get("address") == constants.SOL_MINT
            and (p.get("quoteToken") or {}).get("symbol", "").upper() in ("USDC", "USDT")
        ]
        best = self.best_of(stable_quoted or pairs)
        price = float(best.get("priceUsd") or 0) if best else 0.0
        if price > 0:
            self._sol_price_cache = (price, time.time())
        return price
