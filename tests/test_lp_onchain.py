"""On-chain LP verification — the user's own RPC reads the Raydium v4
pool state and computes locked% (burned + recognized-locker custody).
Core invariant under test: this checker can only ever PROVE a lock; every
unverifiable condition yields None (no verdict), and a below-threshold
reading is ignored by the safety chain rather than trusted as unlock
evidence."""
import pytest

from gftrade import config
from gftrade.discovery.lp_onchain import (INCINERATOR, KNOWN_LOCKER_OWNERS,
                                          RAYDIUM_V4_PROGRAM, V4_STATE_SIZE,
                                          OnchainLp, b58decode, b58encode,
                                          parse_v4_pool)
from gftrade.discovery.safety import SafetyChecker

from conftest import make_pair
from test_safety import CLEAN_MINT_INFO, FakeRugCheck, accounts, clean_rpc

SOL_MINT = "So11111111111111111111111111111111111111112"
BASE_MINT = b58encode(bytes([7]) * 32)
LP_MINT = b58encode(bytes([9]) * 32)
STREAMFLOW = "strmRqUCoQUgGUan5YhzUZa6KqdzwX5L6FpUxfmKg5m"
POOL_ADDR = "PoolAddr111111111111111111111111111111111111"


def test_b58_roundtrips_real_keys():
    for key in (SOL_MINT, INCINERATOR, RAYDIUM_V4_PROGRAM, STREAMFLOW):
        assert b58encode(b58decode(key)) == key
    assert len(b58decode(SOL_MINT)) == 32
    assert b58decode(INCINERATOR)[0] == 0  # leading '1' = leading zero byte


def make_pool_bytes(base=BASE_MINT, quote=SOL_MINT, lp=LP_MINT,
                    lp_reserve=1_000):
    data = bytearray(V4_STATE_SIZE)
    data[400:432] = b58decode(base)
    data[432:464] = b58decode(quote)
    data[464:496] = b58decode(lp)
    data[720:728] = lp_reserve.to_bytes(8, "little")
    return bytes(data)


def test_parse_v4_pool_layout():
    pool = parse_v4_pool(make_pool_bytes(lp_reserve=555))
    assert pool == {"base_mint": BASE_MINT, "quote_mint": SOL_MINT,
                    "lp_mint": LP_MINT, "lp_reserve": 555}
    assert parse_v4_pool(b"\x00" * 100) is None  # wrong size = not v4
    assert parse_v4_pool(None) is None


class PoolRpc:
    """RPC fake with a pool account, an LP mint, and LP holders."""

    def __init__(self, pool_data=None, pool_owner=RAYDIUM_V4_PROGRAM,
                 lp_supply=100, holders=None, holder_owners=None):
        self.pool_data = pool_data if pool_data is not None else make_pool_bytes()
        self.pool_owner = pool_owner
        self.lp_supply = lp_supply
        self.holders = holders or []          # [(address, amount)]
        self.holder_owners = holder_owners or {}  # address -> owner

    async def get_account_raw(self, pubkey):
        if pubkey == POOL_ADDR:
            return self.pool_owner, self.pool_data
        return None, None

    async def get_mint_info(self, mint):
        if mint == LP_MINT:
            return {"decimals": 9, "supply": self.lp_supply,
                    "mint_authority": None, "freeze_authority": None,
                    "owner_program": None}
        return None

    async def get_token_largest_accounts(self, mint):
        return [{"address": a, "amount": str(amt)} for a, amt in self.holders]

    async def get_token_account_owners(self, addresses):
        return [self.holder_owners.get(a) for a in addresses]


def raydium_pair(**overrides):
    pair = make_pair(mint=BASE_MINT, dex_id="raydium")
    pair["pairAddress"] = POOL_ADDR
    pair.update(overrides)
    return pair


async def test_burned_lp_counts_as_locked():
    # 1000 minted, 100 left -> 90% burned
    checker = OnchainLp(PoolRpc(lp_supply=100))
    assert await checker.lp_locked_pct(BASE_MINT, raydium_pair()) == 90.0


async def test_locker_custody_adds_to_burn():
    # 60% burned + 350/1000 held by Streamflow = 95%
    rpc = PoolRpc(lp_supply=400,
                  holders=[("lpacct1", 350), ("lpacct2", 50)],
                  holder_owners={"lpacct1": STREAMFLOW, "lpacct2": "somebody"})
    assert await OnchainLp(rpc).lp_locked_pct(BASE_MINT, raydium_pair()) == 95.0


async def test_incinerator_custody_counts():
    rpc = PoolRpc(lp_supply=1_000, holders=[("lpacct1", 900)],
                  holder_owners={"lpacct1": INCINERATOR})
    assert await OnchainLp(rpc).lp_locked_pct(BASE_MINT, raydium_pair()) == 90.0


async def test_unknown_holders_yield_a_low_number_not_a_crash():
    # nothing burned, nothing recognizably locked -> 0.0 (the chain will
    # ignore a below-threshold reading; see the integration tests)
    rpc = PoolRpc(lp_supply=1_000, holders=[("lpacct1", 990)],
                  holder_owners={"lpacct1": "randomwallet"})
    assert await OnchainLp(rpc).lp_locked_pct(BASE_MINT, raydium_pair()) == 0.0


async def test_no_verdict_conditions():
    good = raydium_pair()
    # non-raydium venue
    assert await OnchainLp(PoolRpc()).lp_locked_pct(
        BASE_MINT, make_pair(mint=BASE_MINT, dex_id="pumpswap")) is None
    # pool owned by some other program (CPMM, spoof, ...)
    assert await OnchainLp(PoolRpc(pool_owner="SomeOtherProgram")).lp_locked_pct(
        BASE_MINT, good) is None
    # pool mints don't match the pair's tokens (misparse guard)
    other = make_pool_bytes(base=b58encode(bytes([1]) * 32))
    assert await OnchainLp(PoolRpc(pool_data=other)).lp_locked_pct(
        BASE_MINT, good) is None
    # zero lpReserve
    assert await OnchainLp(PoolRpc(pool_data=make_pool_bytes(lp_reserve=0))
                           ).lp_locked_pct(BASE_MINT, good) is None
    # supply above reserve = bookkeeping we don't understand
    assert await OnchainLp(PoolRpc(lp_supply=5_000)).lp_locked_pct(
        BASE_MINT, good) is None
    # rpc blowing up -> None, never an exception
    class BoomRpc:
        async def get_account_raw(self, pubkey):
            raise ConnectionError("rpc down")
    assert await OnchainLp(BoomRpc()).lp_locked_pct(BASE_MINT, good) is None


# ---------- integration with the safety chain ----------

def chain(onchain_rpc, rugcheck):
    checker = SafetyChecker(clean_rpc(), rugcheck, onchain=OnchainLp(onchain_rpc))
    checker.MIN_CHECK_INTERVAL = 0.0
    return checker


async def test_onchain_proof_skips_the_apis():
    rugcheck = FakeRugCheck(5.0)  # would banish — must never be consulted
    rugcheck.calls = 0
    real = rugcheck.lp_locked_pct

    async def counting(mint):
        rugcheck.calls += 1
        return await real(mint)

    rugcheck.lp_locked_pct = counting
    report = await chain(PoolRpc(lp_supply=100), rugcheck).check(
        BASE_MINT, pair=raydium_pair())
    assert report.lp_locked_pct == 90.0
    assert report.lp_source == "onchain"
    assert report.passes(strict=True)
    assert rugcheck.calls == 0
    assert "chain" in report.line()


async def test_low_onchain_reading_never_banishes_alone():
    """0% by our math + RugCheck unknown -> ❓ unverified, NOT risky.
    Our locker list can't be exhaustive, so a low reading is not proof."""
    rpc = PoolRpc(lp_supply=1_000, holders=[("lpacct1", 990)],
                  holder_owners={"lpacct1": "unknownlocker"})
    report = await chain(rpc, FakeRugCheck(None)).check(
        BASE_MINT, pair=raydium_pair())
    assert report.lp_locked_pct is None  # no verdict, not "0% = rug"
    assert not report.passes(strict=True)
    assert report.passes(strict=False)


async def test_low_onchain_reading_defers_to_rugcheck():
    rpc = PoolRpc(lp_supply=1_000)  # 0% by our math
    report = await chain(rpc, FakeRugCheck(100.0)).check(
        BASE_MINT, pair=raydium_pair())
    assert report.lp_locked_pct == 100.0
    assert report.lp_source == "rugcheck"
    # ...and a real RugCheck unlock verdict still banishes as before
    report2 = await chain(rpc, FakeRugCheck(3.0)).check(
        b58encode(bytes([8]) * 32), pair=raydium_pair())
    assert not report2.passes(strict=False)


async def test_threshold_is_the_acceptance_bar():
    """A 79% on-chain reading (below MIN_LP_LOCKED_PCT=80) is ignored;
    80%+ is accepted."""
    assert config.MIN_LP_LOCKED_PCT == 80
    just_under = PoolRpc(lp_supply=210)  # 79% burned
    report = await chain(just_under, FakeRugCheck(None)).check(
        BASE_MINT, pair=raydium_pair())
    assert report.lp_locked_pct is None
    just_over = PoolRpc(lp_supply=200)  # 80% burned
    report = await chain(just_over, FakeRugCheck(None)).check(
        b58encode(bytes([6]) * 32), pair=raydium_pair())
    assert report.lp_locked_pct == 80.0
    assert report.lp_source == "onchain"
