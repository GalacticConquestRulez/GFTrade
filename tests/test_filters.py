from gftrade.discovery import filters

from conftest import make_pair


def reasons_for(**kwargs):
    ok, reasons = filters.screen_pair(make_pair(**kwargs))
    return ok, " | ".join(reasons)


def test_good_pair_passes():
    ok, reasons = filters.screen_pair(make_pair())
    assert ok, reasons


def test_rejects_boosted_pair_field():
    ok, reasons = reasons_for(boosts_active=2)
    assert not ok and "boosted" in reasons


def test_rejects_boosted_from_feed():
    pair = make_pair()
    boosted = {("solana", pair["baseToken"]["address"].lower())}
    ok, reasons = filters.screen_pair(pair, boosted)
    assert not ok and "boosted" in reasons[0]


def test_rejects_wrong_quote_token():
    ok, reasons = reasons_for(quote_symbol="BONK")
    assert not ok and "quote token" in reasons


def test_rejects_too_new_and_too_old():
    ok, reasons = reasons_for(age_hours=0.1)
    assert not ok and "old, min" in reasons
    ok, reasons = reasons_for(age_hours=48)
    assert not ok and "exceeds max" in reasons


def test_rejects_missing_creation_timestamp():
    pair = make_pair()
    del pair["pairCreatedAt"]
    ok, reasons = filters.screen_pair(pair)
    assert not ok and any("timestamp" in r for r in reasons)


def test_rejects_thin_liquidity():
    ok, reasons = reasons_for(liquidity=2_000)
    assert not ok and "below floor" in reasons


def test_rejects_liq_mcap_ratio_out_of_band():
    ok, reasons = reasons_for(market_cap=5_000_000)  # ratio 0.005
    assert not ok and "below min" in reasons
    ok, reasons = reasons_for(liquidity=200_000, market_cap=250_000)  # ratio 0.8
    assert not ok and "above max" in reasons


def test_rejects_dead_volume():
    ok, reasons = reasons_for(vol_h1=100)
    assert not ok and "volume" in reasons


def test_rejects_low_buy_counts():
    ok, reasons = reasons_for(buys_5m=2)
    assert not ok and "buys in 5m" in reasons
    ok, reasons = reasons_for(buys_h1=3)
    assert not ok and "buys in 1h" in reasons


def test_rejects_wash_shaped_imbalance():
    ok, reasons = reasons_for(buys_5m=50, sells_5m=5)
    assert not ok and "wash" in reasons


def test_zero_sells_with_buy_flow_is_honeypot_signature():
    """Many buys and NO sells means there's no evidence anyone can sell —
    the sim would happily record a fake win on such a coin."""
    ok, reasons = reasons_for(buys_h1=60, sells_h1=0, buys_5m=8, sells_5m=3)
    assert not ok and "honeypot signature" in reasons
    # a genuinely quiet coin with few buys isn't accused
    ok, reasons = filters.screen_pair(make_pair(buys_h1=5, sells_h1=0))
    assert "honeypot" not in " | ".join(reasons)


def test_h1_sell_throttle_ratio_rejected():
    ok, reasons = reasons_for(buys_h1=200, sells_h1=10, buys_5m=8, sells_5m=3)
    assert not ok and "throttled sells" in reasons


def test_healthy_two_sided_flow_still_passes():
    ok, reasons = filters.screen_pair(make_pair())
    assert ok, reasons
