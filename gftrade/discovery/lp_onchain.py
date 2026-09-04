"""
On-chain LP lock verification — answering the LP question with the user's
own RPC instead of waiting on rate-limited third parties.

The pipeline (classic Raydium AMM v4 pools, the standard memecoin venue):

  pool account (the pair address)
    -> validate: owned by the Raydium v4 program, exact v4 layout size,
       AND the pool's base/quote mints match the pair we're evaluating
       (a misparse or wrong account can never produce evidence)
    -> lp mint + lpReserve (total LP ever minted, kept by the program)
    -> burned%  = (lpReserve - current LP supply) / lpReserve
       (burned LP is gone forever — the strongest lock there is)
    -> custody: largest remaining LP holders whose owner is the
       incinerator or a recognized locker program count as locked
    -> % of total LP locked = burned + locker-held

Evidence rules (same asymmetry as the rest of the chain): this checker
only ever PROVES a lock. A low reading is NOT trusted as unlock evidence
— our locker list can't be exhaustive, and a coin locked with a locker we
don't recognize must not be banished on our ignorance — so anything below
the caller's threshold falls through to RugCheck/GoPlus exactly as
before. Non-v4 venues (CLMM, Meteora positions, CPMM) return None here
and use the API chain / structural rules instead.

Known limitation, stated honestly: locker custody is counted as locked
without reading each locker's unlock timestamp (per-locker account
layouts — a future refinement). Burned LP needs no timestamp.
"""
import base64
import logging

from .. import config

logger = logging.getLogger(__name__)

# ---------- base58 (no external dependency) ----------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {c: i for i, c in enumerate(_B58_ALPHABET)}


def b58encode(raw: bytes) -> str:
    num = int.from_bytes(raw, "big")
    out = []
    while num > 0:
        num, rem = divmod(num, 58)
        out.append(_B58_ALPHABET[rem])
    pad = 0
    for byte in raw:
        if byte == 0:
            pad += 1
        else:
            break
    return "1" * pad + "".join(reversed(out))


def b58decode(text: str) -> bytes:
    num = 0
    for char in text:
        num = num * 58 + _B58_INDEX[char]  # KeyError on junk = caller's cue
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big")
    pad = 0
    for char in text:
        if char == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


# ---------- Raydium AMM v4 pool state ----------

RAYDIUM_V4_PROGRAM = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
V4_STATE_SIZE = 752
_OFF_BASE_MINT = 400
_OFF_QUOTE_MINT = 432
_OFF_LP_MINT = 464
_OFF_LP_RESERVE = 720

# LP sent here is burned in effect (a common alternative to spl-burn).
INCINERATOR = "1nc1nerator11111111111111111111111111111111"

# Token-account OWNERS whose LP custody counts as locked. Extend this set
# as new lockers earn recognition — an unknown locker is simply not
# counted, which can only make our number lower (safe direction).
KNOWN_LOCKER_OWNERS = {
    INCINERATOR,
    "strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m",   # Streamflow
    "LocpQgucEQHbqNABEYvBvwoxCPsSbG91A1QaQhQQqjn",   # Jupiter Lock
}


def parse_v4_pool(data: bytes):
    """{base_mint, quote_mint, lp_mint, lp_reserve} from a Raydium AMM v4
    liquidity-state account, or None when the bytes aren't that layout."""
    if not isinstance(data, bytes) or len(data) != V4_STATE_SIZE:
        return None
    return {
        "base_mint": b58encode(data[_OFF_BASE_MINT:_OFF_BASE_MINT + 32]),
        "quote_mint": b58encode(data[_OFF_QUOTE_MINT:_OFF_QUOTE_MINT + 32]),
        "lp_mint": b58encode(data[_OFF_LP_MINT:_OFF_LP_MINT + 32]),
        "lp_reserve": int.from_bytes(
            data[_OFF_LP_RESERVE:_OFF_LP_RESERVE + 8], "little"),
    }


class OnchainLp:
    """LP locked % straight from chain state. Never raises; every
    unverifiable condition returns None (= no verdict, consult the APIs)."""

    def __init__(self, rpc):
        self._rpc = rpc

    async def lp_locked_pct(self, mint: str, pair: dict):
        try:
            return await self._compute(pair)
        except Exception:
            logger.debug("onchain LP check failed for %s", mint, exc_info=True)
            return None

    async def lp_locked_pct_many(self, pairs: list) -> dict:
        """{mint: pct_or_None} for many pairs in a handful of batched RPC
        requests instead of up to four per coin.

        Same guards and same arithmetic as the single-pair path — the only
        difference is that each phase reads every pool at once. Any pair
        that fails a guard simply never enters the next phase, so a
        malformed pool costs nothing and yields no verdict."""
        try:
            return await self._compute_many(pairs)
        except Exception:
            logger.debug("batched onchain LP check failed", exc_info=True)
            return {}

    async def _compute_many(self, pairs: list) -> dict:
        # Phase 1 — only classic Raydium v4 pairs have a layout we read.
        candidates = {}
        for pair in pairs:
            if not isinstance(pair, dict):
                continue
            if str(pair.get("dexId") or "").strip().lower() != "raydium":
                continue
            pool_addr = pair.get("pairAddress")
            mint = (pair.get("baseToken") or {}).get("address")
            if pool_addr and mint:
                candidates[pool_addr] = pair
        if not candidates:
            return {}

        # Phase 2 — every pool account in one batched read.
        raws = await self._rpc.get_account_raws(list(candidates))
        pools = {}
        for pool_addr, pair in candidates.items():
            owner, data = raws.get(pool_addr) or (None, None)
            if owner != RAYDIUM_V4_PROGRAM:
                continue
            pool = parse_v4_pool(data)
            if pool is None or pool["lp_reserve"] <= 0:
                continue
            pair_tokens = {
                (pair.get("baseToken") or {}).get("address"),
                (pair.get("quoteToken") or {}).get("address"),
            }
            if {pool["base_mint"], pool["quote_mint"]} != pair_tokens:
                continue  # layout drift or wrong account -> no verdict
            pools[pool["lp_mint"]] = (pair, pool)
        if not pools:
            return {}

        # Phase 3 — every LP mint's supply in one batched read.
        lp_infos = await self._rpc.get_mint_infos(list(pools))
        results, need_holders = {}, []
        for lp_mint, (pair, pool) in pools.items():
            mint = (pair.get("baseToken") or {}).get("address")
            info = lp_infos.get(lp_mint)
            if info is None:
                continue
            supply = int(info.get("supply") or 0)
            reserve = pool["lp_reserve"]
            if supply > reserve:
                continue  # bookkeeping we don't understand -> no verdict
            burned_pct = 100.0 * (reserve - supply) / reserve
            # Burn alone can already clear the bar; when it does, the
            # holder lookups below are two RPC calls that cannot change
            # the answer, so skip them.
            if supply == 0 or burned_pct >= config.MIN_LP_LOCKED_PCT:
                results[mint] = min(burned_pct, 100.0)
            else:
                need_holders.append((lp_mint, mint, reserve, supply))
        if not need_holders:
            return results

        # Phase 4/5 — LP holders, then their owner wallets, batched.
        holder_map = await self._rpc.get_token_largest_accounts_many(
            [lp_mint for lp_mint, _, _, _ in need_holders])
        addresses = []
        for lp_mint, _, _, _ in need_holders:
            addresses.extend(a.get("address") for a in holder_map.get(lp_mint, [])
                             if a.get("address"))
        owners = await self._rpc.get_token_account_owners(addresses[:100]) \
            if addresses else []
        owner_of = dict(zip(addresses, owners))

        for lp_mint, mint, reserve, supply in need_holders:
            locked = reserve - supply
            for account in holder_map.get(lp_mint, []):
                if owner_of.get(account.get("address")) in KNOWN_LOCKER_OWNERS:
                    try:
                        locked += int(account.get("amount") or 0)
                    except (TypeError, ValueError):
                        continue
            results[mint] = 100.0 * min(locked, reserve) / reserve
        return results

    async def _compute(self, pair: dict):
        if not isinstance(pair, dict):
            return None
        if str(pair.get("dexId") or "").strip().lower() != "raydium":
            return None  # v4 layout only; other venues use the API chain
        pool_addr = pair.get("pairAddress")
        if not pool_addr:
            return None

        owner, data = await self._rpc.get_account_raw(pool_addr)
        if owner != RAYDIUM_V4_PROGRAM:
            return None  # CPMM/CLMM/unknown program: not our layout
        pool = parse_v4_pool(data)
        if pool is None:
            return None

        # The pool must be about the tokens the pair says it is — this is
        # the guard that turns any layout drift into "no verdict" instead
        # of a wrong verdict.
        pair_tokens = {
            (pair.get("baseToken") or {}).get("address"),
            (pair.get("quoteToken") or {}).get("address"),
        }
        if {pool["base_mint"], pool["quote_mint"]} != pair_tokens:
            return None

        lp_reserve = pool["lp_reserve"]
        if lp_reserve <= 0:
            return None
        lp_info = await self._rpc.get_mint_info(pool["lp_mint"])
        if lp_info is None:
            return None
        supply = int(lp_info.get("supply") or 0)
        if supply > lp_reserve:
            return None  # bookkeeping we don't understand -> no verdict

        locked = lp_reserve - supply  # burned: supply that no longer exists
        if supply > 0:
            largest = await self._rpc.get_token_largest_accounts(pool["lp_mint"])
            addresses = [a.get("address") for a in largest if a.get("address")]
            owners = await self._rpc.get_token_account_owners(addresses[:20])
            by_address = dict(zip(addresses, owners))
            for account in largest:
                holder = by_address.get(account.get("address"))
                if holder in KNOWN_LOCKER_OWNERS:
                    try:
                        locked += int(account.get("amount") or 0)
                    except (TypeError, ValueError):
                        continue
        return 100.0 * min(locked, lp_reserve) / lp_reserve
