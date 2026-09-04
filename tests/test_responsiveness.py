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


async def test_scan_now_caps_uncached_safety_checks(store, monkeypatch):
    """Budget exhaustion leaves the remainder ❓ rather than hidden. The
    budget is patched to a small number so this tests the behavior, not
    whatever the production constant happens to be."""
    import gftrade.scanner as scanner_mod

    budget = 4
    monkeypatch.setattr(scanner_mod, "SCAN_NOW_SAFETY_BUDGET", budget)
    count = budget + 6
    scanner, safety = build_wide_scanner(store, count)
    verdicts = await scanner.scan_now()
    assert safety.network_calls <= budget
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


async def test_sweep_watchdog_cancels_a_wedged_pass(store):
    """No matter what wedges inside a sweep, the watchdog cancels it,
    reports once, and the next sweep can start — 'last sweep never' can
    no longer persist silently."""
    import asyncio

    scanner, _ = build_wide_scanner(store, 1)
    scanner.SWEEP_TIMEOUT_SECONDS = 0.05

    async def wedged_tick(discover=True, exits=True):
        await asyncio.sleep(30)

    scanner.tick = wedged_tick
    published = []

    async def publish(events):
        published.extend(events)

    await scanner._discovery_pass(publish)  # returns instead of hanging
    assert published and published[0]["type"] == "scan_error"
    assert "watchdog" in published[0]["where"]


async def test_start_shows_first_sweep_in_progress(store):
    from gftrade.tg import formatting as fmt

    summary = {"win_rate": None, "open_positions": 0, "closed_trades": 0,
               "realized_pnl_sol": 0.0}
    import time as _time
    text = fmt.start_text(True, summary, True, 41,
                          last_tick_at=None, sweep_started_at=_time.time() - 40)
    assert "never" not in text
    assert "first one running now" in text
    # once a sweep completed, the normal "Xs ago" wins
    text = fmt.start_text(True, summary, True, 41,
                          last_tick_at=_time.time() - 10,
                          sweep_started_at=_time.time() - 5)
    assert "ago" in text and "running now" not in text


async def test_empty_scan_explains_where_candidates_went(store):
    """41 candidates -> 0 listed must render as a breakdown, not a shrug."""
    from gftrade.tg import formatting as fmt

    text = fmt.scan_page_text(
        [], 0, 5, evaluated=20, banned=3,
        drops={"no_pair": 21, "too_old": 15, "unvetted": 2},
    )
    assert "41 candidates" in text
    assert "21" in text and "no tradable" in text
    assert "15" in text and "aged out" in text
    assert "3 banned" in text
    assert "2" in text and "budget" in text


async def test_scan_now_records_drop_diagnostics(store):
    """Aged-out coins land in the drops breakdown the empty message uses."""
    from gftrade.scanner import Scanner
    from gftrade.trading.engine import TradingEngine
    from conftest import FakeDex, make_pair

    old_mint = "O" * 40 + "oooo"
    pairs = {old_mint: make_pair(mint=old_mint, symbol="OLD", age_hours=40)}
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": old_mint}])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      FakeSafety())
    verdicts = await scanner.scan_now()
    assert verdicts == []
    assert scanner.last_scan["drops"]["too_old"] == 1


async def test_safety_checks_for_different_mints_run_concurrently():
    """The old global lock ran checks one-by-one — a 12-coin sweep took a
    minute. Different mints must overlap now (same-mint dedupe still holds,
    covered in test_safety)."""
    import asyncio
    from gftrade.discovery.safety import SafetyChecker
    from test_safety import clean_rpc

    class SlowLpSource:
        def __init__(self):
            self.in_flight = 0
            self.max_in_flight = 0

        async def lp_locked_pct(self, mint):
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            await asyncio.sleep(0.05)
            self.in_flight -= 1
            return 100.0

    slow = SlowLpSource()
    checker = SafetyChecker(clean_rpc(), slow)
    checker.MIN_CHECK_INTERVAL = 0.0
    await asyncio.gather(*(checker.check(f"M{i}" + "x" * 40) for i in range(6)))
    assert slow.max_in_flight > 1  # overlapped, not serialized


async def test_scan_now_prefetches_safety_concurrently(store):
    """scan_now warms the cache with overlapping checks instead of paying
    for each one inside the sequential evaluation loop."""
    import asyncio

    class SlowConcurrencyProbe(FakeSafety):
        def __init__(self):
            super().__init__()
            self.in_flight = 0
            self.max_in_flight = 0

        async def check(self, mint, pair=None):
            if mint not in self._cache:
                self.in_flight += 1
                self.max_in_flight = max(self.max_in_flight, self.in_flight)
                await asyncio.sleep(0.02)
                self.in_flight -= 1
            return await super().check(mint, pair=pair)

    from gftrade.scanner import Scanner
    from gftrade.trading.engine import TradingEngine

    pairs = {mint_r(i): make_strong_pair(mint=mint_r(i), symbol=f"HOT{i}")
             for i in range(5)}
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m} for m in pairs])
    safety = SlowConcurrencyProbe()
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True), safety)
    verdicts = await scanner.scan_now()
    assert safety.max_in_flight > 1
    assert all(v["safety_ok"] for v in verdicts)  # prefetch fed the loop


async def test_background_sweep_keeps_scan_list_warm(store):
    """A discovery tick must leave a ready-to-render /scan cache behind, so
    the Scan button serves instantly instead of sweeping for a minute."""
    scanner, _ = build_wide_scanner(store, 3)
    assert scanner.last_scan is None
    await scanner.tick()
    cache = scanner.last_scan
    assert cache is not None and len(cache["verdicts"]) == 3
    assert all(v["safety_ok"] for v in cache["verdicts"])
    assert "drops" in cache and "at" in cache


async def test_background_scan_cache_still_banishes_risky(store):
    """The warm cache is a display surface like any other: known-risky
    coins must never appear in it."""
    from gftrade.scanner import Scanner
    from gftrade.trading.engine import TradingEngine
    from gftrade.discovery.safety import SafetyReport

    class RiskySafety(FakeSafety):
        async def check(self, mint, pair=None):
            self.check_calls += 1
            report = SafetyReport(mint=mint, mint_renounced=True, freeze_none=True,
                                  top10_pct=10.0, lp_locked_pct=2.0,  # unlocked
                                  standard_token=True)
            self._cache[mint] = report
            return report

    pair = make_strong_pair()
    dex = FakeDex(pairs_by_mint={MINT_A: pair},
                  profiles=[{"chainId": "solana", "tokenAddress": MINT_A}])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      RiskySafety())
    await scanner.tick()
    cache = scanner.last_scan
    assert cache["verdicts"] == []
    assert cache["banned"] == 1


async def test_scan_serves_fresh_cache_instantly(store):
    import time as _time
    from types import SimpleNamespace
    from gftrade.tg.handlers import scan_cache_fresh

    rows = [{"mint": "x"}]
    deps = SimpleNamespace(scanner=SimpleNamespace(last_scan=None))
    assert not scan_cache_fresh(deps)  # no cache -> must sweep
    deps.scanner.last_scan = {"verdicts": rows, "at": _time.time() - 30}
    assert scan_cache_fresh(deps)  # 30s old with rows -> instant render
    deps.scanner.last_scan = {"verdicts": rows, "at": _time.time() - 200}
    assert not scan_cache_fresh(deps)  # stale -> live sweep


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


async def test_empty_cache_is_never_served_as_fresh(store):
    """An empty list is the one result worth re-checking live: serving it
    from cache reports 'nothing listable' without having looked."""
    import time as _time
    from types import SimpleNamespace
    from gftrade.tg.handlers import scan_cache_fresh

    deps = SimpleNamespace(scanner=SimpleNamespace(
        last_scan={"verdicts": [], "at": _time.time()}))
    assert not scan_cache_fresh(deps)
    deps.scanner.last_scan = {"verdicts": [{"mint": "x"}], "at": _time.time()}
    assert scan_cache_fresh(deps)


async def test_background_sweep_vets_near_misses_so_the_list_fills(store):
    """Near-misses fail the market screens, so the alert path's prefetch
    never touches them. The sweep must vet them itself or a market with
    no screen-passers leaves /scan permanently empty."""
    from gftrade.scanner import Scanner
    from gftrade.trading.engine import TradingEngine

    # every coin fails the liquidity screen -> all are near-misses
    pairs = {mint_r(i): make_pair(mint=mint_r(i), symbol=f"THIN{i}",
                                  liquidity=4_000, market_cap=40_000,
                                  chg_h1=5 + i)
             for i in range(12)}
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m} for m in pairs])
    safety = FakeSafety()
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True), safety)

    await scanner.tick()
    listed = scanner.last_scan["verdicts"]
    assert listed, "a sweep with no screen-passers must still fill from near-misses"
    assert all(v["safety"] is not None for v in listed)  # nothing unvetted shown

    # and successive sweeps converge toward a full list as the cache warms
    before = len(listed)
    await scanner.tick()
    assert len(scanner.last_scan["verdicts"]) >= before
