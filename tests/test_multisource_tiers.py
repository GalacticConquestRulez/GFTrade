"""Two-source pricing (GeckoTerminal failover) and the quality/risk split."""
import pytest

from gftrade.clients.geckoterminal import parse_simple_prices
from gftrade.discovery.safety import SafetyReport, risk_tier
from gftrade.discovery.scoring import market_score_pair, score_pair
from gftrade.scanner import Scanner
from gftrade.trading.engine import TradingEngine

from conftest import (GOOD_SAFETY, MINT_A, FakeDex, FakeSafety, make_pair,
                      make_strong_pair)

RISKY = SafetyReport(mint="x", mint_renounced=True, freeze_none=True,
                     top10_pct=10.0, lp_locked_pct=4.0, standard_token=True)
UNKNOWN = SafetyReport(mint="x", mint_renounced=True, freeze_none=True,
                       top10_pct=10.0, lp_locked_pct=None, standard_token=True)


# ---------- quality/risk split ----------

def test_market_score_ignores_safety_entirely():
    pair = make_strong_pair()
    market = market_score_pair(pair)
    assert 0 <= market <= 100
    combined_safe, _ = score_pair(pair, GOOD_SAFETY, strict=True)
    combined_risky, _ = score_pair(pair, RISKY, strict=True)
    assert combined_safe != combined_risky      # combined score moves with safety
    assert market_score_pair(pair) == market    # market score does not


def test_risk_tier_classification():
    assert risk_tier(GOOD_SAFETY) == "safe"
    assert risk_tier(RISKY) == "risky"
    assert risk_tier(UNKNOWN) == "unverified"
    assert risk_tier(None) == "unverified"
    assert risk_tier(SafetyReport(mint="x", freeze_none=False)) == "risky"


async def test_scan_sorts_by_tier_before_market_quality(store):
    """A risky coin with the hottest chart must still list below every
    safe and unverified coin — risk and quality no longer share a number."""
    mints = {"safe": "S" * 40 + "ssss", "unv": "U" * 40 + "uuuu",
             "risk": "R" * 40 + "rrrr"}
    reports = {
        mints["safe"]: GOOD_SAFETY,
        mints["unv"]: UNKNOWN,
        mints["risk"]: RISKY,
    }

    class TierSafety(FakeSafety):
        async def check(self, mint):
            self.check_calls += 1
            report = SafetyReport(**{**reports[mint].__dict__, "mint": mint})
            self._cache[mint] = report
            return report

    pairs = {
        mints["safe"]: make_strong_pair(mint=mints["safe"], symbol="SAFE",
                                        chg_h1=12.0),
        mints["unv"]: make_strong_pair(mint=mints["unv"], symbol="UNV",
                                       chg_h1=18.0),
        # riskiest coin gets the strongest chart on purpose
        mints["risk"]: make_strong_pair(mint=mints["risk"], symbol="RISK",
                                        chg_h1=29.0, vol_m5=15_000),
    }
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m}
                            for m in pairs])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      TierSafety())
    verdicts = await scanner.scan_now()
    # the risky coin — despite the hottest chart — is banished, not ranked
    assert [v["risk_tier"] for v in verdicts] == ["safe", "unverified"]
    assert all(v["mint"] != mints["risk"] for v in verdicts)
    assert scanner.last_scan["banned"] == 1


# ---------- gecko pricing ----------

def test_parse_simple_prices_defensively():
    data = {"data": {"attributes": {"token_prices": {
        MINT_A: "0.00123", "BAD": "not-a-number", "ZERO": "0",
    }}}}
    assert parse_simple_prices(data) == {MINT_A: 0.00123}
    assert parse_simple_prices({}) == {}
    assert parse_simple_prices(None) == {}


class DownDex(FakeDex):
    """DexScreener that fails pair lookups but still serves the SOL price
    (which is cache-backed in the real client)."""

    async def pairs_for_tokens(self, chain_id, addresses):
        raise ConnectionError("429 too many requests")


class FakeGeckoPrices:
    def __init__(self, prices):
        self.prices = prices

    async def simple_token_prices(self, mints):
        return {m: self.prices[m] for m in mints if m in self.prices}


async def test_exits_fail_over_to_gecko_prices(store):
    """DexScreener rate-limited mid-position: the stop must still fire,
    priced from GeckoTerminal."""
    healthy = FakeDex(pairs_by_mint={MINT_A: make_pair()})
    engine = TradingEngine(store, healthy, dry_run=True)
    await engine.buy(MINT_A, 0.3)
    sl_price = store.get_position(MINT_A)["sl_price_usd"]

    down = DownDex(pairs_by_mint={})
    failover_engine = TradingEngine(
        store, down, dry_run=True,
        gecko=FakeGeckoPrices({MINT_A: sl_price * 0.9}),
    )
    events = await failover_engine.check_exits()
    assert events and events[0]["reason"] == "stop_loss"
    assert store.get_position(MINT_A) is None


async def test_exits_without_fallback_still_raise(store):
    healthy = FakeDex(pairs_by_mint={MINT_A: make_pair()})
    engine = TradingEngine(store, healthy, dry_run=True)
    await engine.buy(MINT_A, 0.1)
    down_engine = TradingEngine(store, DownDex(pairs_by_mint={}), dry_run=True)
    with pytest.raises(ConnectionError):
        await down_engine.check_exits()


async def test_checkpoint_prices_prefer_gecko_and_backfill_from_dex(store):
    dex = FakeDex(pairs_by_mint={"MISSING": make_pair(mint="MISSING",
                                                      price_usd=0.5)})
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      FakeSafety(), gecko=FakeGeckoPrices({MINT_A: 0.002}))
    prices = await scanner._fetch_prices([MINT_A, "MISSING"])
    assert prices[MINT_A] == 0.002       # from gecko
    assert prices["MISSING"] == 0.5      # backfilled from dexscreener


async def test_risky_near_misses_cannot_fill_the_list(store):
    """Near-misses skip safety during screening, but nothing reaches the
    visible list without proving it isn't known-bad — a thin market must
    not become a backdoor for unlocked-LP coins."""
    class RiskySafety(FakeSafety):
        async def check(self, mint):
            self.check_calls += 1
            report = SafetyReport(mint=mint, mint_renounced=True, freeze_none=True,
                                  top10_pct=10.0, lp_locked_pct=2.0,
                                  standard_token=True)
            self._cache[mint] = report
            return report

    # every coin fails the liquidity screen -> all are near-miss candidates
    pairs = {f"NM{i:02d}" + "w" * 36: make_pair(mint=f"NM{i:02d}" + "w" * 36,
                                                liquidity=4_000, market_cap=40_000)
             for i in range(6)}
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m} for m in pairs])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      RiskySafety())
    verdicts = await scanner.scan_now()
    assert verdicts == []                       # nothing clean to show
    assert scanner.last_scan["banned"] == 6     # and the shield says why


async def test_clean_near_misses_still_fill(store):
    pairs = {f"CL{i:02d}" + "w" * 36: make_pair(mint=f"CL{i:02d}" + "w" * 36,
                                                liquidity=4_000, market_cap=40_000)
             for i in range(4)}
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m} for m in pairs])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True),
                      FakeSafety())
    verdicts = await scanner.scan_now()
    assert len(verdicts) == 4
    assert all(v["risk_tier"] != "risky" for v in verdicts)


def test_known_bad_card_loses_one_tap_buys():
    from gftrade.tg.keyboards import token_kb
    safe_kb = token_kb(MINT_A, [0.1, 0.5], known_bad=False)
    safe_callbacks = [b.callback_data for row in safe_kb.inline_keyboard for b in row]
    assert any((c or "").startswith("b:") for c in safe_callbacks)

    bad_kb = token_kb(MINT_A, [0.1, 0.5], known_bad=True)
    bad_callbacks = [b.callback_data for row in bad_kb.inline_keyboard for b in row]
    assert not any((c or "").startswith("b:") for c in bad_callbacks)  # no presets
    assert any((c or "").startswith("bc:") for c in bad_callbacks)     # typed only


def test_risk_reasons_are_specific():
    from gftrade.tg import formatting as fmt
    report = SafetyReport(mint="x", mint_renounced=False, freeze_none=True,
                          top10_pct=10.0, lp_locked_pct=3.0, standard_token=True)
    reasons = " | ".join(fmt.risk_reasons(report))
    assert "print supply" in reasons
    assert "pull the pool" in reasons
    assert fmt.risk_reasons(GOOD_SAFETY) == []


def test_banned_count_renders_in_header():
    from gftrade.tg import formatting as fmt
    from conftest import make_strong_pair as msp
    verdict = {"pair": msp(), "mint": MINT_A, "score": 80, "market_score": 80,
               "breakdown": {}, "patterns": [], "safety": GOOD_SAFETY,
               "safety_ok": True, "screened_ok": True, "reject_reasons": []}
    text = fmt.scan_page_text([verdict], 0, 5, evaluated=10, banned=3)
    assert "🛡 3 known-risky removed" in text


async def test_dexscreener_batch_isolates_poison_mints():
    """A 500 on one batch (the live failure the owners hit) must not kill
    the whole lookup: the batch splits until the poison mint is isolated
    and only it is dropped."""
    import httpx
    from gftrade.clients.dexscreener import DexScreener

    poison = "POISON" + "x" * 38
    good = [f"OK{i:02d}" + "y" * 38 for i in range(3)]

    class SplittingDex(DexScreener):
        def __init__(self):
            super().__init__(client=None)
            self.calls = []

        async def _get(self, path, params=None):
            self.calls.append(path)
            if poison in path:
                raise httpx.HTTPStatusError("500", request=None, response=None)
            batch = path.rsplit("/", 1)[1].split(",")
            return [{"chainId": "solana",
                     "baseToken": {"address": m}, "priceUsd": "1"} for m in batch]

    dex = SplittingDex()
    pairs = await dex.pairs_for_tokens("solana", good + [poison])
    got = {p["baseToken"]["address"] for p in pairs}
    assert got == set(good)          # every clean mint survived
    assert len(dex.calls) >= 3       # it actually split to isolate


async def test_dexscreener_total_failure_still_raises_for_failover():
    from gftrade.clients.dexscreener import DexScreener

    class DeadDex(DexScreener):
        def __init__(self):
            super().__init__(client=None)

        async def _get(self, path, params=None):
            raise ConnectionError("down")

    with pytest.raises(Exception):
        await DeadDex().pairs_for_tokens("solana", ["A" * 40 + "aaaa"])
