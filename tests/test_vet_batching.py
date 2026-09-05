"""Batched vetting: the same verdicts, in a fraction of the requests.

Wall-clock can't be measured meaningfully in a sandbox, so the speedup is
pinned by HTTP REQUEST COUNT — the thing that actually scales with the
rate limit. A 140-coin sweep must cost roughly a dozen requests, not the
~840 it used to.

The other half of this file is the property that makes the optimisation
safe: batched and unbatched vetting must reach identical conclusions,
because only the fetching changed — every evidence rule still runs per
coin in _check_uncached."""
import json

import httpx
import pytest

from gftrade import config
from gftrade.discovery.lp_onchain import OnchainLp
from gftrade.discovery.safety import SafetyChecker
from gftrade.solana_rpc import SolanaRpc

from conftest import make_pair
from test_lp_onchain import (BASE_MINT, LP_MINT, POOL_ADDR, PoolRpc,
                             make_pool_bytes, raydium_pair)
from test_safety import CLEAN_MINT_INFO, FakeRugCheck, accounts


def mint_account(supply="1000"):
    return {"owner": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
            "data": {"parsed": {"type": "mint",
                                "info": {"decimals": 9, "supply": supply,
                                         "mintAuthority": None,
                                         "freezeAuthority": None}}}}


class CountingTransport:
    """MockTransport that answers plausibly and counts HTTP requests."""

    def __init__(self):
        self.requests = 0

    def handler(self, request):
        self.requests += 1
        payload = json.loads(request.content)
        entries = payload if isinstance(payload, list) else [payload]
        out = []
        for entry in entries:
            out.append({"jsonrpc": "2.0", "id": entry["id"],
                        "result": self._result(entry)})
        return httpx.Response(200, json=out if isinstance(payload, list) else out[0])

    def _result(self, entry):
        method, params = entry["method"], entry.get("params") or []
        if method == "getMultipleAccounts":
            return {"value": [mint_account() for _ in params[0]]}
        if method == "getAccountInfo":
            return {"value": mint_account()}
        if method == "getTokenLargestAccounts":
            return {"value": [{"address": "acc0", "amount": "500"},
                              {"address": "acc1", "amount": "40"}]}
        return {"value": None}


def build_checker(transport, mints_are_raydium=False):
    client = httpx.AsyncClient(transport=httpx.MockTransport(transport.handler))
    rpc = SolanaRpc("https://primary.example/rpc", client, max_rps=0)
    checker = SafetyChecker(rpc, FakeRugCheck(None),
                            onchain=OnchainLp(rpc) if mints_are_raydium else None)
    checker.MIN_CHECK_INTERVAL = 0.0
    return checker


def curve_pairs(count):
    """Bonding-curve pairs: LP resolves locally, so this isolates the
    cost of the authority + holder reads."""
    return [make_pair(mint=f"C{i:03d}" + "k" * 37, dex_id="pumpfun")
            for i in range(count)]


async def test_140_coins_cost_about_a_dozen_requests():
    transport = CountingTransport()
    checker = build_checker(transport)
    await checker.prefetch_many(curve_pairs(140))
    # 2 getMultipleAccounts (100 + 40) + 2 batched holder arrays = ~4.
    # The old path was 2 per coin = 280.
    assert transport.requests <= 12, transport.requests
    # and every coin actually got vetted
    assert sum(1 for p in curve_pairs(140)
               if checker.cached((p["baseToken"])["address"]) is not None) == 140


async def test_batched_and_unbatched_reach_the_same_verdict():
    """Only fetching changed, so conclusions must not."""
    pairs = curve_pairs(6)

    batched = build_checker(CountingTransport())
    await batched.prefetch_many(pairs)

    single = build_checker(CountingTransport())
    for pair in pairs:
        await single.check(pair["baseToken"]["address"], pair=pair)

    for pair in pairs:
        mint = pair["baseToken"]["address"]
        a, b = batched.cached(mint), single.cached(mint)
        assert a is not None and b is not None
        assert (a.mint_renounced, a.freeze_none, a.standard_token,
                a.top10_pct, a.lp_locked_pct, a.lp_source) == \
               (b.mint_renounced, b.freeze_none, b.standard_token,
                b.top10_pct, b.lp_locked_pct, b.lp_source)


async def test_prefetch_skips_already_cached_mints():
    transport = CountingTransport()
    checker = build_checker(transport)
    pairs = curve_pairs(10)
    await checker.prefetch_many(pairs)
    first = transport.requests
    await checker.prefetch_many(pairs)  # all cached now
    assert transport.requests == first  # no further network at all


async def test_missing_batch_data_never_becomes_a_verdict():
    """If the batch can't answer for a mint, that mint must come back
    unverified — never accidentally complete."""
    class EmptyTransport(CountingTransport):
        def _result(self, entry):
            if entry["method"] == "getMultipleAccounts":
                return {"value": [None for _ in (entry["params"] or [[]])[0]]}
            return {"value": None}

    transport = EmptyTransport()
    checker = build_checker(transport)
    pairs = curve_pairs(4)
    await checker.prefetch_many(pairs)
    for pair in pairs:
        report = checker.cached(pair["baseToken"]["address"])
        assert report is not None
        assert report.mint_renounced is None   # unknown, not assumed good
        assert not report.passes(strict=True)  # can never autobuy
        assert report.passes(strict=False)     # but not condemned either


async def test_batch_rpc_failure_leaves_everything_unverified():
    class DeadTransport(CountingTransport):
        def handler(self, request):
            self.requests += 1
            raise httpx.ConnectError("rpc down")

    checker = build_checker(DeadTransport())
    pairs = curve_pairs(3)
    await checker.prefetch_many(pairs)
    for pair in pairs:
        report = checker.cached(pair["baseToken"]["address"])
        assert report is None or not report.passes(strict=True)


# ---------- batched on-chain LP ----------

async def test_batched_lp_matches_the_single_pair_result():
    rpc = PoolRpc(lp_supply=100)  # 1000 minted, 100 left -> 90% burned
    onchain = OnchainLp(rpc)
    single = await onchain.lp_locked_pct(BASE_MINT, raydium_pair())
    many = await onchain.lp_locked_pct_many([raydium_pair()])
    assert single == 90.0
    assert many == {BASE_MINT: 90.0}


async def test_batched_lp_skips_holder_reads_when_burn_already_proves_it():
    """Once burn clears the threshold the holder lookups cannot change the
    answer, so they are two RPC calls not worth making."""
    calls = []

    class TrackingRpc(PoolRpc):
        async def get_token_largest_accounts_many(self, mints):
            calls.append(mints)
            return await super().get_token_largest_accounts_many(mints)

    rpc = TrackingRpc(lp_supply=100)  # 90% burned, over the 80% bar
    assert await OnchainLp(rpc).lp_locked_pct_many([raydium_pair()]) == {
        BASE_MINT: 90.0}
    assert calls == []  # never asked


async def test_batched_lp_guards_still_reject_mismatched_pools():
    other = make_pool_bytes(base="9" * 43 + "x")
    rpc = PoolRpc(pool_data=other)
    assert await OnchainLp(rpc).lp_locked_pct_many([raydium_pair()]) == {}


async def test_batched_lp_ignores_non_raydium_pairs():
    rpc = PoolRpc()
    pairs = [make_pair(mint=BASE_MINT, dex_id="pumpswap"),
             make_pair(mint=BASE_MINT, dex_id="pumpfun")]
    assert await OnchainLp(rpc).lp_locked_pct_many(pairs) == {}


async def test_failed_holder_read_never_looks_like_perfect_distribution():
    """Regression: an empty holder list from a failed batch entry used to
    compute top10_pct = 0.0 — which reads as ideal distribution and let a
    coin nobody verified reach the safe tier and become autobuy-eligible.
    An empty result is not an answer; it must stay unknown."""
    from gftrade import constants
    from gftrade.discovery.safety import SafetyChecker

    class HolderBatchDown:
        """Mint info resolves; every holder read comes back empty."""
        def __init__(self):
            self.individual_retries = 0

        async def get_mint_infos(self, mints):
            return {m: {"decimals": 9, "supply": 1000, "mint_authority": None,
                        "freeze_authority": None,
                        "owner_program": constants.TOKEN_PROGRAM_ID}
                    for m in mints}

        async def get_token_largest_accounts_many(self, mints):
            return {m: [] for m in mints}      # what a failed batch returns

        async def get_token_largest_accounts(self, mint):
            self.individual_retries += 1
            return []                           # the retry fails too

        async def get_mint_info(self, mint):
            raise AssertionError("mint info was prefetched; should not refetch")

    rpc = HolderBatchDown()
    checker = SafetyChecker(rpc, None)
    checker.MIN_CHECK_INTERVAL = 0.0
    mint = "H" * 44
    await checker.prefetch_many([{"dexId": "pumpfun",
                                  "baseToken": {"address": mint}}])
    report = checker.cached(mint)
    assert report.top10_pct is None          # unknown, not 0.0
    assert not report.passes(strict=True)    # cannot autobuy
    assert report.passes(strict=False)       # but not condemned either
    assert rpc.individual_retries == 1       # empty batch triggers one retry


async def test_real_holder_data_still_computes_concentration():
    """The guard must not swallow a legitimate reading."""
    from gftrade import constants
    from gftrade.discovery.safety import SafetyChecker

    class GoodRpc:
        async def get_mint_infos(self, mints):
            return {m: {"decimals": 9, "supply": 1000, "mint_authority": None,
                        "freeze_authority": None,
                        "owner_program": constants.TOKEN_PROGRAM_ID}
                    for m in mints}

        async def get_token_largest_accounts_many(self, mints):
            # largest (the pool) is dropped; next ten sum to 100/1000 = 10%
            return {m: [{"address": "pool", "amount": "500"},
                        {"address": "w1", "amount": "60"},
                        {"address": "w2", "amount": "40"}] for m in mints}

    checker = SafetyChecker(GoodRpc(), None)
    checker.MIN_CHECK_INTERVAL = 0.0
    mint = "G" * 44
    await checker.prefetch_many([{"dexId": "pumpfun",
                                  "baseToken": {"address": mint}}])
    assert checker.cached(mint).top10_pct == 10.0


# ---------- third-party API budget ----------

class SlowApi:
    """Stand-in for RugCheck/GoPlus: counts how many coins reach it."""
    def __init__(self):
        self.calls = 0

    async def lp_locked_pct(self, mint):
        self.calls += 1
        return None  # can't answer -> coin stays unverified


async def test_paced_apis_are_bounded_per_sweep():
    """RugCheck/GoPlus pace requests 1.2s and 2.1s apart, so an unbounded
    sweep lets a slow external service decide how long our sweep takes.
    Past the budget, coins keep lp unknown and retry next sweep."""
    from gftrade import constants
    from gftrade.discovery.safety import SafetyChecker

    class Rpc:
        async def get_mint_infos(self, mints):
            return {m: {"decimals": 9, "supply": 1000, "mint_authority": None,
                        "freeze_authority": None,
                        "owner_program": constants.TOKEN_PROGRAM_ID}
                    for m in mints}

        async def get_token_largest_accounts_many(self, mints):
            return {m: [{"address": "pool", "amount": "900"},
                        {"address": "w", "amount": "50"}] for m in mints}

    api = SlowApi()
    checker = SafetyChecker(Rpc(), api)
    checker.MIN_CHECK_INTERVAL = 0.0
    checker.start_sweep(api_budget=3)

    # 10 raydium coins: none resolve locally, so all would reach the API
    pairs = [make_pair(mint=f"R{i:03d}" + "j" * 37, dex_id="raydium")
             for i in range(10)]
    await checker.prefetch_many(pairs)

    assert api.calls == 3, api.calls  # budget honoured
    # Every coin still got a report; the over-budget ones are unverified.
    reports = [checker.cached(p["baseToken"]["address"]) for p in pairs]
    assert all(r is not None for r in reports)
    assert all(not r.passes(strict=True) for r in reports)   # LP unknown
    assert all(r.passes(strict=False) for r in reports)      # not condemned


async def test_api_budget_is_unlimited_without_a_sweep():
    """The token-card path checks one coin on demand; a few seconds there
    is fine, so it must not inherit a sweep's budget."""
    from gftrade import constants
    from gftrade.discovery.safety import SafetyChecker

    class Rpc:
        async def get_mint_info(self, mint):
            return {"decimals": 9, "supply": 0, "mint_authority": None,
                    "freeze_authority": None,
                    "owner_program": constants.TOKEN_PROGRAM_ID}

    api = SlowApi()
    checker = SafetyChecker(Rpc(), api)
    checker.MIN_CHECK_INTERVAL = 0.0
    for i in range(5):
        await checker.check(f"T{i:03d}" + "m" * 37,
                            pair=make_pair(dex_id="raydium"))
    assert api.calls == 5  # no budget applied


async def test_start_sweep_resets_the_budget():
    from gftrade.discovery.safety import SafetyChecker

    checker = SafetyChecker(None, SlowApi())
    checker.start_sweep(api_budget=2)
    assert checker._spend_api_budget() is True
    assert checker._spend_api_budget() is True
    assert checker._spend_api_budget() is False   # spent
    checker.start_sweep(api_budget=2)
    assert checker._spend_api_budget() is True    # fresh allowance


async def test_browse_list_vetting_never_spends_the_api_budget(store):
    """The 11.6s-of-a-12.4s-sweep fix: near-misses failed the market
    screens so they can't alert or be bought whatever their LP says.
    Vetting them must not consume paced third-party calls — the whole
    allowance belongs to the alert path."""
    from gftrade.scanner import Scanner
    from gftrade.trading.engine import TradingEngine
    from conftest import FakeDex, FakeSafety, make_pair as mp

    class BudgetSpy(FakeSafety):
        def __init__(self):
            super().__init__()
            self._api_budget = None
            self.budgets_seen = []

        def start_sweep(self, api_budget=None):
            self._api_budget = api_budget
            self.budgets_seen.append(api_budget)

        async def prefetch_many(self, pairs):
            # record what the budget was while the browse list was vetted
            self.budgets_seen.append(("prefetch", self._api_budget))
            await super().prefetch_many(pairs)

    # every coin fails the screens -> all are near-misses
    pairs = {f"N{i:03d}" + "v" * 37: mp(mint=f"N{i:03d}" + "v" * 37,
             liquidity=4_000, market_cap=40_000) for i in range(12)}
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m} for m in pairs])
    safety = BudgetSpy()
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True), safety)
    await scanner.tick()

    prefetches = [b for b in safety.budgets_seen if isinstance(b, tuple)]
    assert prefetches, "the browse list should have been vetted"
    assert all(budget == 0 for _, budget in prefetches), prefetches

    # The allowance is zeroed only for the browse phase and restored after,
    # so the next sweep's alert path still gets its full budget.
    scalars = [b for b in safety.budgets_seen if not isinstance(b, tuple)]
    assert scalars[-1] == scalars[0], scalars   # ends as it began
    assert 0 in scalars                          # and was zeroed in between
    assert scanner.last_scan["verdicts"], "list still fills"
