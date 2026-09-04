"""The /filters diagnostic: where candidates actually die.

"It feels too selective" is only answerable with counts, so every
evaluated candidate is tallied into the stage that stopped it, and every
market-screen rejection is bucketed by which screen fired."""
import pytest

from gftrade.discovery import filters
from gftrade.scanner import Scanner
from gftrade.tg import formatting as fmt
from gftrade.trading.engine import TradingEngine

from conftest import MINT_A, FakeDex, FakeSafety, make_pair, make_strong_pair


def test_every_screen_reason_lands_in_a_real_bucket():
    """Guards against the classifier drifting from the reason strings:
    a reason that fell through to 'other' would silently hide a screen
    from the diagnostic."""
    hostile = {
        "chainId": "solana", "baseToken": {"address": "x"},
        "quoteToken": {"symbol": "BONK"},
        "liquidity": {"usd": 10}, "marketCap": 0, "volume": {"h1": 1},
        "txns": {"m5": {"buys": 1, "sells": 0}, "h1": {"buys": 12, "sells": 0}},
        "boosts": {"active": 2},
    }
    _, reasons = filters.screen_pair(hostile)
    assert reasons
    for reason in reasons:
        assert filters.classify_reason(reason) != "other", reason

    # the age and ratio branches need their own shapes to fire
    for pair, expected in (
        (make_pair(age_hours=0.05), "too young"),
        (make_pair(age_hours=99), "too old"),
        (make_pair(liquidity=1_000_000, market_cap=1_000_000),
         "liquidity vs market-cap band"),
        (make_pair(liquidity=1_000, market_cap=10_000_000),
         "liquidity vs market-cap band"),
        (make_pair(buys_5m=40, sells_5m=1), "buy/sell ratio (wash-trading cap)"),
    ):
        _, reasons = filters.screen_pair(pair)
        codes = {filters.classify_reason(r) for r in reasons}
        assert expected in codes, (expected, reasons)
        assert "other" not in codes


async def test_funnel_counts_every_candidate_once(store):
    """Coins that pass and coins that fail all land in exactly one stage."""
    pairs = {
        MINT_A: make_strong_pair(),                                   # alerts
        "T" * 44: make_pair(mint="T" * 44, liquidity=100),            # screens
        "U" * 44: make_pair(mint="U" * 44, age_hours=99),             # aged out
    }
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m} for m in pairs])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      FakeSafety())
    await scanner.tick()
    funnel = scanner.last_tick_stats["funnel"]
    # every mint considered lands in exactly one stage — including the
    # ones dropped before evaluation, which is where the age window bites
    assert sum(funnel.values()) == 3
    assert funnel.get("alerted") == 1
    assert funnel.get("market screens") == 1
    assert funnel.get("aged out of window") == 1
    rejects = scanner.last_tick_stats["screen_rejects"]
    assert rejects.get("liquidity below floor") == 1


async def test_funnel_separates_safety_from_screens(store):
    """A coin that passes the market screens but can't prove safety is
    reported as 'safety unproven', not lumped in with the screens."""
    from gftrade.discovery.safety import SafetyReport

    class UnprovableSafety(FakeSafety):
        async def check(self, mint, pair=None):
            self.check_calls += 1
            report = SafetyReport(mint=mint)  # everything unknown
            self._cache[mint] = report
            return report

    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()},
                  profiles=[{"chainId": "solana", "tokenAddress": MINT_A}])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      UnprovableSafety())
    await scanner.tick()
    assert scanner.last_tick_stats["funnel"].get("safety unproven") == 1
    assert not scanner.last_tick_stats["screen_rejects"]


async def test_funnel_flags_the_extension_gate(store):
    """The extension gate is invisible in /scan but silently blocks
    alerts, so it gets its own funnel stage."""
    from gftrade.discovery.trend import PriceHistory

    import time as _time
    now = _time.time()
    history = PriceHistory()
    # >=3 points spanning >=10min, ending +400% above the low
    for offset, price in ((-1800, 0.0002), (-900, 0.0005), (-60, 0.001)):
        history.record(MINT_A, price, ts=now + offset)
    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()},
                  profiles=[{"chainId": "solana", "tokenAddress": MINT_A}])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      FakeSafety(), prices=history)
    await scanner.tick()
    assert scanner.last_tick_stats["funnel"].get("too extended") == 1


def test_filters_text_renders_the_funnel_and_thresholds(store):
    stats = {"checked": 40, "funnel": {"market screens": 30, "alerted": 2,
                                       "safety unproven": 8},
             "screen_rejects": {"too old": 12, "liquidity below floor": 9}}
    text = fmt.filters_text(stats, store.settings, pool_size=120)
    assert "40 candidates evaluated" in text and "120 in pool" in text
    assert "market screens" in text and "30" in text
    assert "too old" in text and "12" in text
    assert "alert score" in text  # thresholds shown alongside
    assert "loosen first" in text


def test_filters_text_before_any_sweep(store):
    assert "No sweep has completed" in fmt.filters_text({}, store.settings, 0)


def test_filters_text_shows_per_phase_timing(store):
    """The 5-10s target has to be measurable from Telegram, not inferred."""
    stats = {"checked": 140, "funnel": {"alerted": 3},
             "screen_rejects": {},
             "timing": {"feeds": 0.4, "dexscreener": 0.6, "safety": 2.1,
                        "evaluate": 0.2, "scan-list": 0.1}}
    text = fmt.filters_text(stats, store.settings, pool_size=140)
    assert "3.4s</b> total" in text
    assert "safety 2.1s" in text
    assert "dexscreener 0.6s" in text


async def test_sweep_records_timing_for_every_phase(store):
    from gftrade.scanner import Scanner
    from gftrade.trading.engine import TradingEngine

    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()},
                  profiles=[{"chainId": "solana", "tokenAddress": MINT_A}])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      FakeSafety())
    await scanner.tick()
    timing = scanner.last_tick_stats["timing"]
    assert set(timing) == {"feeds", "dexscreener", "safety", "evaluate",
                           "scan-list"}
    assert all(v >= 0 for v in timing.values())
