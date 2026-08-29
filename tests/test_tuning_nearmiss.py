"""Runtime-tunable screens and the /scan near-miss fill: the list should
never be uselessly empty, and thresholds should be adjustable from
Telegram without touching the server."""
import pytest

from gftrade.discovery import filters
from gftrade.scanner import Scanner
from gftrade.tg import formatting as fmt
from gftrade.tg.handlers import _parse_setting
from gftrade.trading.engine import TradingEngine

from conftest import MINT_A, FakeDex, FakeSafety, make_pair, make_strong_pair


def mint_n(i):
    return f"N{i:02d}" + "y" * 38


def test_screen_overrides_replace_config_thresholds():
    thin = make_pair(liquidity=4_000, market_cap=40_000)  # fails default $10k floor
    ok, reasons = filters.screen_pair(thin)
    assert not ok and any("liquidity" in r for r in reasons)
    ok, _ = filters.screen_pair(thin, overrides={"min_liquidity_usd": 3_000})
    assert ok
    old = make_pair(age_hours=20)  # fails default 12h max age
    assert not filters.screen_pair(old)[0]
    assert filters.screen_pair(old, overrides={"max_pair_age_hours": 24.0})[0]


def build_thin_market_scanner(store, count=12):
    """A market where every coin fails the liquidity screen."""
    pairs = {
        mint_n(i): make_pair(mint=mint_n(i), symbol=f"THIN{i}",
                             liquidity=4_000, market_cap=40_000,
                             chg_h1=5 + i)  # varying scores
        for i in range(count)
    }
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": m} for m in pairs])
    engine = TradingEngine(store, dex, dry_run=True)
    return Scanner(store, dex, engine, FakeSafety()), dex


async def test_scan_fills_with_ranked_near_misses(store):
    scanner, _ = build_thin_market_scanner(store)
    verdicts = await scanner.scan_now()
    assert len(verdicts) == Scanner.SCAN_MIN_LIST  # never uselessly empty
    assert all(not v["screened_ok"] for v in verdicts)
    scores = [v["score"] for v in verdicts]
    assert scores == sorted(scores, reverse=True)  # best to worst
    assert all(v["reject_reasons"] for v in verdicts)
    # rendering shows the near-miss badge and the tuning hint
    text = fmt.scan_page_text(verdicts, 0, 5, evaluated=12)
    assert "🔻" in text and "/settings" in text
    assert "liquidity" in text  # the actual reason is visible


async def test_screened_coins_rank_above_near_misses(store):
    scanner, dex = build_thin_market_scanner(store, count=6)
    dex.pairs_by_mint[MINT_A] = make_strong_pair()  # one real passer
    dex.profiles.append({"chainId": "solana", "tokenAddress": MINT_A})
    verdicts = await scanner.scan_now()
    assert verdicts[0]["mint"] == MINT_A
    assert verdicts[0]["screened_ok"] is True
    assert all(not v["screened_ok"] for v in verdicts[1:])


async def test_aged_out_coins_never_shown_even_as_near_misses(store):
    old_mint = mint_n(99)
    pairs = {old_mint: make_pair(mint=old_mint, symbol="OLD", age_hours=40)}
    dex = FakeDex(pairs_by_mint=pairs,
                  profiles=[{"chainId": "solana", "tokenAddress": old_mint}])
    scanner = Scanner(store, dex, TradingEngine(store, dex, dry_run=True), FakeSafety())
    assert await scanner.scan_now() == []


async def test_loosened_settings_take_effect_immediately(store):
    """Lowering the liquidity floor from /settings turns near-misses into
    full screen-passers on the next sweep — no restart needed."""
    scanner, _ = build_thin_market_scanner(store, count=3)
    verdicts = await scanner.scan_now()
    assert all(not v["screened_ok"] for v in verdicts)
    store.set_setting("min_liquidity_usd", 3_000.0)
    verdicts = await scanner.scan_now()
    assert all(v["screened_ok"] for v in verdicts)


def test_new_screen_setting_parsers():
    assert _parse_setting("min_liquidity_usd", "$5,000") == 5000.0
    assert _parse_setting("min_volume_h1_usd", "2500") == 2500.0
    assert _parse_setting("min_buys_h1", "10") == 10
    assert _parse_setting("max_pair_age_hours", "24") == 24.0
    with pytest.raises(ValueError):
        _parse_setting("max_pair_age_hours", "0.5")
    with pytest.raises(ValueError):
        _parse_setting("min_buys_h1", "5000")


def test_start_text_shows_stats_feeds_and_version():
    summary = {"open_positions": 0, "closed_trades": 0, "win_rate": None,
               "realized_pnl_sol": 0.0, "sim_balance_sol": 1.0}
    text = fmt.start_text(
        True, summary, True, 250, last_tick_at=None,
        tick_stats={"checked": 118, "passed_screen": 2, "signals": 1},
        feed_status={"profiles": "ok", "new-pools": "error"},
        version="1.3.0",
    )
    assert "v1.3.0" in text
    assert "118 checked" in text and "2 passed screens" in text
    assert "profiles ✓" in text and "new-pools ✗" in text


def test_min_age_override_including_zero():
    young = make_pair(age_hours=0.1)  # 6 minutes old
    ok, reasons = filters.screen_pair(young)
    assert not ok and any("old, min" in r for r in reasons)
    assert filters.screen_pair(young, overrides={"min_pair_age_minutes": 5.0})[0]
    brand_new = make_pair(age_hours=0.01)  # ~36 seconds
    assert filters.screen_pair(brand_new, overrides={"min_pair_age_minutes": 0.0})[0]


async def test_lowered_min_age_enables_autobuy_on_young_coins(store):
    """The reported gap: strong coins under 20 minutes old never reached
    autobuy. With the setting lowered they must flow all the way through."""
    from gftrade.trading.engine import TradingEngine as TE

    young = make_strong_pair(age_hours=0.15)  # 9 minutes old
    dex = FakeDex(pairs_by_mint={MINT_A: young},
                  profiles=[{"chainId": "solana", "tokenAddress": MINT_A}])
    engine = TE(store, dex, dry_run=True)
    scanner = Scanner(store, dex, engine, FakeSafety())
    store.set_setting("autobuy", True)

    # default 20m minimum: screened out, no autobuy
    events = await scanner.tick()
    assert [e for e in events if e["type"] in ("signal", "autobuy")] == []
    assert store.positions == {}

    # lowered to 5m: the same coin autobuys on the next sweep
    store.set_setting("min_pair_age_minutes", 5.0)
    events = await scanner.tick()
    assert len([e for e in events if e["type"] == "autobuy"]) == 1
    assert store.get_position(MINT_A) is not None


def test_min_age_parser_bounds():
    assert _parse_setting("min_pair_age_minutes", "0") == 0.0
    assert _parse_setting("min_pair_age_minutes", "10") == 10.0
    with pytest.raises(ValueError):
        _parse_setting("min_pair_age_minutes", "500")
