"""
Minimal async Solana JSON-RPC client over httpx. Only the handful of
methods this bot needs — read-only account/token queries, transaction
submission, and confirmation polling. Keeping this thin (instead of
depending on solana-py) avoids the historically fragile solana/solders
version matrix; solders alone handles keys and transaction signing.
"""
import asyncio
import base64
import itertools
import logging
import time

import httpx

from . import config

logger = logging.getLogger(__name__)


class RpcError(Exception):
    pass


class RateLimiter:
    """Client-side requests-per-second ceiling.

    RPC providers cap requests per second (Helius: 10/s free, 50/s on the
    $49 Developer plan), and the bot's own call volume is bursty — a
    discovery pass verifying 8 fresh coins fires ~6 calls each. Pacing
    upstream by staggering *checks* only works if you know how many calls
    a check makes, which changes whenever the checks change; limiting
    where the requests actually leave is the version that stays correct.

    Each caller reserves the next free slot under the lock, then sleeps
    outside it, so N concurrent callers queue and wake in order instead of
    serializing behind one another's sleeps."""

    def __init__(self, rps: float):
        self._interval = 1.0 / rps if rps and rps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self, slots: int = 1) -> None:
        """Reserve `slots` worth of rate budget. A JSON-RPC array counts as
        one request on most providers but as N on some, so batching passes
        the count it should be charged (see config.RPC_BATCH_COUNTS_AS)."""
        if not self._interval:
            return
        cost = self._interval * max(1, slots)
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_at)
            self._next_at = slot + cost
        delay = slot - now
        if delay > 0:
            await asyncio.sleep(delay)


class SolanaRpc:
    """JSON-RPC client with an optional standby endpoint.

    `fallback_url` exists so the primary can be a fast-but-newer endpoint
    (Helius's Gatekeeper edge gateway, say) without that being a single
    point of failure: transport errors on the primary retry once on the
    fallback, and after RPC_FAILOVER_AFTER consecutive primary failures
    calls skip straight to the fallback for a cooldown rather than paying
    the primary's timeout every time. An RpcError (the node answered, it
    just said no) is a real answer and never triggers failover."""

    def __init__(self, url: str, client: httpx.AsyncClient,
                 fallback_url: str = None, max_rps: float = None):
        self.url = url
        self.fallback_url = fallback_url or None
        self._limiter = RateLimiter(
            config.RPC_MAX_RPS if max_rps is None else max_rps)
        self._client = client
        self._ids = itertools.count(1)
        self._primary_failures = 0
        self._primary_paused_until = 0.0

    def _endpoints(self) -> list:
        if not self.fallback_url:
            return [self.url]
        if (self._primary_failures >= config.RPC_FAILOVER_AFTER
                and time.time() < self._primary_paused_until):
            return [self.fallback_url]   # primary is sin-binned; skip its timeout
        return [self.url, self.fallback_url]

    async def _post(self, url: str, payload, slots: int = 1):
        await self._limiter.acquire(slots)
        resp = await self._client.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        return resp.json()

    async def _request(self, payload, slots: int = 1):
        """POST a JSON-RPC payload (object or array) with failover, and
        return the parsed body. Shared by single and batched calls so both
        get identical endpoint, retry and sin-binning behavior."""
        endpoints = self._endpoints()
        last_error = None
        for index, url in enumerate(endpoints):
            try:
                body = await self._post(url, payload, slots)
            except Exception as exc:  # transport/HTTP failure -> try the standby
                last_error = exc
                if url == self.url:
                    self._primary_failures += 1
                    if self._primary_failures >= config.RPC_FAILOVER_AFTER:
                        self._primary_paused_until = (
                            time.time() + config.RPC_FAILOVER_COOLDOWN_SECONDS)
                        logger.warning(
                            "primary RPC failed %d times; using the fallback for %ds",
                            self._primary_failures, config.RPC_FAILOVER_COOLDOWN_SECONDS)
                if index + 1 < len(endpoints):
                    continue
                raise
            if url == self.url:
                self._primary_failures = 0  # healthy again
            return body
        raise last_error

    async def _call(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        body = await self._request(payload)
        if isinstance(body, dict) and "error" in body:
            raise RpcError(f"{method}: {body['error']}")
        return (body or {}).get("result")

    async def _call_batch(self, calls: list) -> list:
        """Send many calls as one JSON-RPC array; return results aligned to
        `calls`, with None wherever an entry failed.

        Two properties matter and are easy to get wrong:
        - Responses may come back in ANY order, so they are matched by id,
          never by position. A server that reorders would otherwise hand
          one mint's answer to another mint — a correctness bug that looks
          like random data corruption.
        - A per-entry error is isolated to that entry (None) rather than
          failing the batch, so one poison mint cannot blank a whole sweep.
        Chunked to RPC_BATCH_SIZE; an entirely failed chunk yields Nones
        for its slots instead of raising, since callers treat a missing
        answer as "unknown" and unknown is always safe here."""
        if not calls:
            return []
        results = [None] * len(calls)
        size = max(1, config.RPC_BATCH_SIZE)
        for start in range(0, len(calls), size):
            chunk = calls[start:start + size]
            by_id = {}
            payload = []
            for offset, call in enumerate(chunk):
                request_id = next(self._ids)
                by_id[request_id] = start + offset
                payload.append({"jsonrpc": "2.0", "id": request_id,
                                "method": call["method"],
                                "params": call.get("params") or []})
            slots = len(chunk) if config.RPC_BATCH_COUNTS_AS == "size" else 1
            try:
                body = await self._request(payload, slots)
            except Exception:
                logger.warning("rpc batch of %d failed; treating as unknown",
                               len(chunk), exc_info=True)
                continue
            if not isinstance(body, list):
                logger.warning("rpc batch returned %s, not an array",
                               type(body).__name__)
                continue
            for entry in body:
                if not isinstance(entry, dict):
                    continue
                index = by_id.get(entry.get("id"))
                if index is None:
                    continue  # unknown id — ignore rather than misattribute
                if "error" in entry:
                    logger.debug("rpc batch entry error: %s", entry["error"])
                    continue
                results[index] = entry.get("result")
        return results

    # ---------- reads ----------

    async def get_balance_sol(self, pubkey: str) -> float:
        result = await self._call("getBalance", [pubkey])
        return result["value"] / 1_000_000_000

    @staticmethod
    def _parse_mint_value(value):
        """One getAccountInfo `value` -> mint dict, or None. Shared by the
        single and batched readers so both interpret bytes identically."""
        if not value:
            return None
        parsed = ((value.get("data") or {}).get("parsed") or {})
        if parsed.get("type") != "mint":
            return None
        info = parsed.get("info") or {}
        return {
            "decimals": info.get("decimals"),
            "supply": int(info.get("supply") or 0),
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            # which token program owns the mint (classic SPL vs Token-2022)
            "owner_program": value.get("owner"),
        }

    @staticmethod
    def _parse_raw_value(value):
        """One getAccountInfo `value` -> (owner_program, data_bytes)."""
        if not value:
            return None, None
        data = value.get("data")
        raw = b""
        if isinstance(data, list) and data and isinstance(data[0], str):
            raw = base64.b64decode(data[0])
        return value.get("owner"), raw

    async def get_mint_info(self, mint: str):
        """Parsed SPL mint account: decimals, supply, and whether the mint /
        freeze authorities still exist (None = renounced, the good case)."""
        result = await self._call(
            "getAccountInfo", [mint, {"encoding": "jsonParsed"}]
        )
        return self._parse_mint_value((result or {}).get("value"))

    async def get_token_largest_accounts(self, mint: str) -> list:
        """Top ~20 token accounts for a mint: [{address, amount(str raw)}...]."""
        result = await self._call("getTokenLargestAccounts", [mint])
        return (result or {}).get("value") or []

    async def get_account_raw(self, pubkey: str):
        """(owner_program, data_bytes) for any account, or (None, None) when
        it doesn't exist. Used to parse non-token program state (AMM pools)."""
        result = await self._call(
            "getAccountInfo", [pubkey, {"encoding": "base64"}]
        )
        return self._parse_raw_value((result or {}).get("value"))

    # ---------- batched reads ----------
    #
    # These exist because vetting a coin costs several account reads, and
    # doing that one HTTP request at a time is what made a 140-coin sweep
    # take minutes. getMultipleAccounts fans 100 accounts into one call;
    # getTokenLargestAccounts has no multi form, so it rides a JSON-RPC
    # array instead. A missing answer is always returned as None — callers
    # read that as "unknown", which is the safe direction.

    async def _multiple_accounts(self, addresses: list, encoding: str) -> list:
        """`value` entries for many accounts, aligned to `addresses`."""
        if not addresses:
            return []
        size = 100  # getMultipleAccounts hard cap
        calls = [{"method": "getMultipleAccounts",
                  "params": [addresses[i:i + size], {"encoding": encoding}]}
                 for i in range(0, len(addresses), size)]
        values = []
        for chunk_index, result in enumerate(await self._call_batch(calls)):
            chunk = calls[chunk_index]["params"][0]
            got = (result or {}).get("value") or []
            # Pad a short/absent response so alignment with `addresses`
            # holds no matter what came back.
            values.extend(list(got)[:len(chunk)]
                          + [None] * max(0, len(chunk) - len(got)))
        return values

    async def get_mint_infos(self, mints: list) -> dict:
        """{mint: mint_info_dict_or_None} for many mints."""
        values = await self._multiple_accounts(mints, "jsonParsed")
        return {mint: self._parse_mint_value(value)
                for mint, value in zip(mints, values)}

    async def get_account_raws(self, addresses: list) -> dict:
        """{address: (owner_program, data_bytes)} for many accounts."""
        values = await self._multiple_accounts(addresses, "base64")
        return {address: self._parse_raw_value(value)
                for address, value in zip(addresses, values)}

    async def get_token_largest_accounts_many(self, mints: list) -> dict:
        """{mint: [{address, amount}, ...]} for many mints."""
        if not mints:
            return {}
        calls = [{"method": "getTokenLargestAccounts", "params": [mint]}
                 for mint in mints]
        results = await self._call_batch(calls)
        return {mint: ((result or {}).get("value") or [])
                for mint, result in zip(mints, results)}

    async def get_token_account_owners(self, addresses: list) -> list:
        """The owner wallet of each SPL token account, aligned with the
        input list; None where an entry is missing or not a token account.
        getMultipleAccounts caps at 100 addresses — callers pass fewer."""
        if not addresses:
            return []
        result = await self._call(
            "getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}]
        )
        owners = []
        for value in (result or {}).get("value") or []:
            owner = None
            if isinstance(value, dict):
                parsed = ((value.get("data") or {}).get("parsed") or {})
                if parsed.get("type") == "account":
                    owner = (parsed.get("info") or {}).get("owner")
            owners.append(owner)
        return owners

    async def get_token_balance(self, owner: str, mint: str):
        """Sum of the owner's token accounts for `mint`.
        Returns (raw_amount:int, decimals:int|None, ui_amount:float)."""
        result = await self._call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        raw, decimals = 0, None
        for entry in (result or {}).get("value") or []:
            info = entry["account"]["data"]["parsed"]["info"]
            token_amount = info["tokenAmount"]
            raw += int(token_amount["amount"])
            decimals = token_amount["decimals"]
        ui = raw / (10 ** decimals) if decimals is not None else 0.0
        return raw, decimals, ui

    # ---------- writes ----------

    async def send_raw_transaction_b64(self, signed_tx_b64: str) -> str:
        return await self._call(
            "sendTransaction",
            [signed_tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
        )

    async def confirm_transaction(self, signature: str, timeout_seconds: int = 60) -> bool:
        """Poll until the tx is confirmed/finalized without an error.
        Returns False on timeout or on-chain error."""
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            result = await self._call(
                "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
            )
            status = ((result or {}).get("value") or [None])[0]
            if status is not None:
                if status.get("err") is not None:
                    return False
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(2)
        return False
