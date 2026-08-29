"""Staged exits: partial take-profit, then a breakeven-floored runner."""
import pytest

from conftest import MINT_A, make_pair


async def test_tp_takes_half_then_runner_stop_locks_gains(engine, dex, store):
    # defaults: tp_sell_pct 50, runner_trailing_pct 20; entry 0.001, TP 0.00135
    await engine.buy(MINT_A, 0.5)
    tokens_before = store.get_position(MINT_A)["token_amount"]

    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.0014)
    events = await engine.check_exits()
    assert events[0]["type"] == "exit_partial"
    assert events[0]["reason"] == "take_profit"
    assert events[0]["pct"] == 50.0
    position = store.get_position(MINT_A)
    assert position is not None
    assert position["tp_taken"] is True
    assert position["token_amount"] == pytest.approx(tokens_before / 2)
    assert position["sol_received"] > 0

    # price keeps running: runner stop trails 20% off the peak, no exit yet
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.003)
    assert await engine.check_exits() == []

    # pulls back >20% from the 0.003 peak -> runner closes, well in profit
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.0023)
    events = await engine.check_exits()
    assert events[0]["type"] == "exit"
    assert events[0]["reason"] == "runner_stop"
    assert events[0]["pnl_sol"] > 0
    assert store.get_position(MINT_A) is None
    # total recovered = TP partial + runner close, comfortably above cost
    assert events[0]["sol_received"] > events[0]["sol_spent"]


async def test_runner_floor_never_triggers_below_entry(engine, dex, store):
    """With a huge runner trail the peak-based stop would sit far below
    entry — the breakeven floor must take over and fire near entry."""
    store.set_setting("runner_trailing_pct", 90.0)
    await engine.buy(MINT_A, 0.5)
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.0014)  # TP partial
    events = await engine.check_exits()
    assert events[0]["type"] == "exit_partial"

    # 0.0011 is above peak*(1-90%)=0.00014 but... above the entry floor too:
    # no exit while price stays over entry
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.0011)
    assert await engine.check_exits() == []

    # dips to entry -> the floor fires even though the trail is far lower
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.00099)
    events = await engine.check_exits()
    assert events[0]["reason"] == "runner_stop"
    assert store.get_position(MINT_A) is None
    # half sold at +40%, half at ~breakeven: whole trade stays green
    assert events[0]["pnl_sol"] > 0


async def test_stop_loss_still_full_exit_before_tp(engine, dex, store):
    """Staged exits change nothing before TP: a stop-loss closes 100%."""
    await engine.buy(MINT_A, 0.5)
    sl_price = store.get_position(MINT_A)["sl_price_usd"]
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=sl_price * 0.95)
    events = await engine.check_exits()
    assert events[0]["type"] == "exit"
    assert events[0]["reason"] == "stop_loss"
    assert store.get_position(MINT_A) is None


async def test_tp_sell_pct_100_is_classic_full_close(engine, dex, store):
    store.set_setting("tp_sell_pct", 100.0)
    await engine.buy(MINT_A, 0.5)
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.0014)
    events = await engine.check_exits()
    assert events[0]["type"] == "exit"
    assert events[0]["reason"] == "take_profit"
    assert store.get_position(MINT_A) is None


def test_new_setting_bounds():
    from gftrade.tg.handlers import _parse_setting
    assert _parse_setting("tp_sell_pct", "50") == 50.0
    assert _parse_setting("runner_trailing_pct", "20") == 20.0
    with pytest.raises(ValueError):
        _parse_setting("tp_sell_pct", "5")
    with pytest.raises(ValueError):
        _parse_setting("runner_trailing_pct", "99")
