"""Structural LP-lock inference — the venues where an LP pull is impossible
by construction (pump.fun / LaunchLab bonding curves, pump.fun's PumpSwap
migration pools). The inference is strictly the LAST rung of the LP chain:
it fires only when RugCheck and GoPlus both answered unknown, it never
overrides real evidence, and it never vouches for plain Raydium pools —
those can always be rugged and must prove their lock the normal way."""
from gftrade.discovery.safety import SafetyChecker, structural_lp_lock

from conftest import MINT_A, make_pair
from test_safety import FakeRugCheck, clean_rpc

PUMP_MINT = "P" * 40 + "pump"


def make_checker(rugcheck_pct):
    checker = SafetyChecker(clean_rpc(), FakeRugCheck(rugcheck_pct))
    checker.MIN_CHECK_INTERVAL = 0.0
    return checker


# ---------- the inference rules ----------

def test_bonding_curves_are_structurally_locked():
    """No LP tokens exist on a bonding curve — nothing to pull."""
    for dex_id in ("pumpfun", "launchlab"):
        result = structural_lp_lock(make_pair(dex_id=dex_id))
        assert result == (100.0, "curve")


def test_pumpswap_migration_pool_is_locked_for_pump_mints():
    result = structural_lp_lock(make_pair(mint=PUMP_MINT, dex_id="pumpswap"))
    assert result == (100.0, "pumpfun")


def test_pumpswap_pool_of_non_pump_mint_is_not_inferred():
    # a permissionless PumpSwap pool for some arbitrary token proves nothing
    assert structural_lp_lock(make_pair(mint=MINT_A, dex_id="pumpswap")) is None


def test_plain_raydium_is_never_inferred_even_with_pump_suffix():
    """A scammer can vanity-grind a mint ending in 'pump' and list it
    straight on Raydium with unlocked LP — the suffix alone proves
    nothing. Raydium pools go through RugCheck/GoPlus like everyone."""
    assert structural_lp_lock(make_pair(dex_id="raydium")) is None
    assert structural_lp_lock(make_pair(mint=PUMP_MINT, dex_id="raydium")) is None


def test_garbage_pairs_are_not_inferred():
    assert structural_lp_lock(None) is None
    assert structural_lp_lock({}) is None
    assert structural_lp_lock({"dexId": None, "baseToken": None}) is None


# ---------- integration with the safety chain ----------

async def test_unknown_lp_rescued_by_curve_structure():
    """RugCheck unknown + pump.fun curve pair -> LP proven locked, coin
    can reach the ✅ safe tier (this was the gap that hid these coins)."""
    checker = make_checker(rugcheck_pct=None)
    pair = make_pair(mint=PUMP_MINT, dex_id="pumpfun")
    report = await checker.check(PUMP_MINT, pair=pair)
    assert report.lp_locked_pct == 100.0
    assert report.lp_source == "curve"
    assert report.passes(strict=True)
    assert "curve" in report.line()


async def test_real_evidence_always_beats_structure():
    """RugCheck says 5% locked -> the structural fallback must NOT launder
    that into 100%. Evidence beats assumption, the coin stays banished."""
    checker = make_checker(rugcheck_pct=5.0)
    pair = make_pair(mint=PUMP_MINT, dex_id="pumpswap")
    report = await checker.check(PUMP_MINT, pair=pair)
    assert report.lp_locked_pct == 5.0
    assert report.lp_source == "rugcheck"
    assert not report.passes(strict=False)  # known-bad -> risky tier


async def test_no_pair_no_inference():
    checker = make_checker(rugcheck_pct=None)
    report = await checker.check(PUMP_MINT)  # token card path without pair data
    assert report.lp_locked_pct is None


async def test_raydium_pair_still_unknown_when_sources_fail():
    checker = make_checker(rugcheck_pct=None)
    report = await checker.check(MINT_A, pair=make_pair(dex_id="raydium"))
    assert report.lp_locked_pct is None  # stays ❓ — strict mode keeps rejecting


async def test_scanner_threads_pair_into_safety(store):
    """End to end: a pump.fun curve coin that both LP APIs can't answer
    still comes out ✅ safe from the scanner's own evaluate_pair."""
    from gftrade.scanner import Scanner
    from gftrade.trading.engine import TradingEngine
    from conftest import FakeDex, make_strong_pair

    pair = make_strong_pair(mint=PUMP_MINT, dex_id="pumpfun")
    dex = FakeDex(pairs_by_mint={PUMP_MINT: pair})
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      make_checker(rugcheck_pct=None))
    verdict = await scanner.evaluate_pair(pair, set())
    assert verdict["safety"].lp_locked_pct == 100.0
    assert verdict["safety_ok"] is True
    assert verdict["risk_tier"] == "safe"


# ---------- bonding curves resolve before the paced APIs ----------

def test_bonding_curve_lock_covers_only_curves():
    """The pre-API short-circuit is narrower than the full structural
    rule: only venues where LP tokens cannot exist."""
    from gftrade.discovery.safety import bonding_curve_lock

    for dex_id in ("pumpfun", "launchlab"):
        assert bonding_curve_lock(make_pair(dex_id=dex_id)) == (100.0, "curve")
    # PumpSwap has real LP tokens, so a third party could hold genuine
    # evidence about it — it must NOT short-circuit ahead of the APIs.
    assert bonding_curve_lock(make_pair(mint=PUMP_MINT, dex_id="pumpswap")) is None
    assert bonding_curve_lock(make_pair(dex_id="raydium")) is None
    assert bonding_curve_lock(None) is None
    assert bonding_curve_lock({}) is None


async def test_curve_coin_never_touches_the_slow_apis():
    """The point of the reorder: a bonding-curve coin resolves locally and
    pays none of RugCheck's 1.2s or GoPlus's 2.1s pacing."""
    class CountingRugCheck(FakeRugCheck):
        def __init__(self):
            super().__init__(100.0)
            self.calls = 0

        async def lp_locked_pct(self, mint):
            self.calls += 1
            return await super().lp_locked_pct(mint)

    rugcheck = CountingRugCheck()
    checker = SafetyChecker(clean_rpc(), rugcheck)
    checker.MIN_CHECK_INTERVAL = 0.0
    report = await checker.check(PUMP_MINT, pair=make_pair(mint=PUMP_MINT,
                                                           dex_id="pumpfun"))
    assert report.lp_locked_pct == 100.0
    assert report.lp_source == "curve"
    assert rugcheck.calls == 0  # never consulted


async def test_pumpswap_still_lets_real_evidence_win():
    """PumpSwap stays behind the APIs, so a real unlock verdict still
    banishes the coin rather than being pre-empted by structure."""
    checker = SafetyChecker(clean_rpc(), FakeRugCheck(4.0))
    checker.MIN_CHECK_INTERVAL = 0.0
    report = await checker.check(PUMP_MINT, pair=make_pair(mint=PUMP_MINT,
                                                           dex_id="pumpswap"))
    assert report.lp_locked_pct == 4.0
    assert report.lp_source == "rugcheck"
    assert not report.passes(strict=False)  # known-bad -> risky
