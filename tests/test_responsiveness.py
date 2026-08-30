"""Bot responsiveness under slow safety sources — the "buttons don't work"
regression. A discovery pass or /scan sweep must never stall for minutes on
paced network safety checks: each pass spends a bounded budget of UNCACHED
checks and leaves the rest at ❓ to retry next pass, and cached verdicts are
always free."""
import pytest

from gftrade.scanner import (MAX_UNCACHED_SAFETY_PER_PASS,
                             SCAN_NOW_SAFETY_BUDGET, Scanner)
from gftrade.trading.engine import TradingEngine

from conftest import MINT_A, FakeDex, FakeSafety, make_pair, make_strong_pair


def mint_r(i):
    return f"B{i:02d}" + "z" * 38


def build_wide_scanner(store, count):
    """A market with `count` strong (screen-passing) coins."""
    pairs = {
        mint_r(i): make_strong_pair(mint=mint_r(i), symbol=f"HOT{i}",
                                    chg_h1=5 + i)
        for i in range(count)
    }
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m} for m in pairs])
    engine = TradingEngine(store, dex, dry_run=True)
    safety = FakeSafety()
    return Scanner(store, dex, engine, safety), safety


async def test_exhausted_budget_leaves_coin_unverified_without_network(store):
    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()})
    safety = FakeSafety()
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True), safety)
    verdict = await scanner.evaluate_pair(make_strong_pair(), set(),
                                          safety_budget=[0])
    assert safety.check_calls == 0  # no network hit at all
    assert verdict["safety"] is None
    assert verdict["safety_ok"] is False
    assert verdict["risk_tier"] == "unverified"  # ❓ this pass, retried next
    assert verdict["score"] > 0  # still scored on market data


async def test_cached_mint_is_free_even_with_no_budget(store):
    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()})
    safety = FakeSafety()
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True), safety)
    await safety.check(MINT_A)  # warm the cache
    verdict = await scanner.evaluate_pair(make_strong_pair(), set(),
                                          safety_budget=[0])
    assert verdict["safety"] is not None
    assert verdict["safety_ok"] is True
    assert verdict["risk_tier"] == "safe"


async def test_budget_counts_down_only_for_uncached_mints(store):
    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()})
    safety = FakeSafety()
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True), safety)
    budget = [2]
    await scanner.evaluate_pair(make_strong_pair(), set(), safety_budget=budget)
    assert budget == [1]  # first look was uncached
    await scanner.evaluate_pair(make_strong_pair(), set(), safety_budget=budget)
    assert budget == [1]  # second look hit the cache — free


async def test_discovery_pass_caps_uncached_safety_checks(store):
    count = MAX_UNCACHED_SAFETY_PER_PASS + 5
    scanner, safety = build_wide_scanner(store, count)
    await scanner.tick()
    assert safety.network_calls == MAX_UNCACHED_SAFETY_PER_PASS
    # next pass picks up where this one stopped: the cache carries over,
    # so repeated sweeps converge on full coverage instead of re-paying
    await scanner.tick()
    assert safety.network_calls == count  # the remaining 5, cache hits free


async def test_scan_now_caps_uncached_safety_checks(store):
    count = SCAN_NOW_SAFETY_BUDGET + 6
    scanner, safety = build_wide_scanner(store, count)
    verdicts = await scanner.scan_now()
    assert safety.network_calls <= SCAN_NOW_SAFETY_BUDGET
    assert len(verdicts) == count  # over-budget coins still listed, as ❓
    unverified = [v for v in verdicts if v["safety"] is None]
    assert unverified  # the over-budget remainder shows as ❓, not hidden


async def test_near_miss_fill_stops_when_budget_spent(store):
    """Near-miss fill must not blow past the sweep's safety budget:
    'unshown beats unvetted' — nothing enters the list unvetted, and
    nothing stalls the sweep to vet it."""
    pairs = {
        mint_r(i): make_pair(mint=mint_r(i), symbol=f"THIN{i}",
                             liquidity=4_000, market_cap=40_000, chg_h1=5 + i)
        for i in range(SCAN_NOW_SAFETY_BUDGET + 8)
    }
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m} for m in pairs])
    safety = FakeSafety()
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True), safety)
    verdicts = await scanner.scan_now()
    assert safety.network_calls <= SCAN_NOW_SAFETY_BUDGET
    # near-misses that were vetted still fill the list up to the usual floor
    assert 0 < len(verdicts) <= Scanner.SCAN_MIN_LIST
    assert all(v["safety"] is not None for v in verdicts)  # nothing unvetted shown


async def test_safety_checker_cached_semantics():
    from gftrade.discovery.safety import SafetyChecker
    from test_safety import FakeRugCheck, clean_rpc

    checker = SafetyChecker(clean_rpc(), FakeRugCheck(100.0))
    checker.MIN_CHECK_INTERVAL = 0.0
    assert checker.cached("MINT") is None  # never checked -> would hit network
    report = await checker.check("MINT")
    assert checker.cached("MINT") is report  # fresh cache -> instant
    checker._cache["MINT"] = (report, 0.0, 1.0)  # simulate expiry
    assert checker.cached("MINT") is None  # stale = not cached
