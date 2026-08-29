import pytest

from gftrade import config
from gftrade.trading.engine import TradeError, TradingEngine, price_in_sol

from conftest import MINT_A, MINT_B, SOL_PRICE, FakeDex, make_pair


async def test_dry_buy_opens_position_with_correct_math(engine, store):
    result = await engine.buy(MINT_A, 0.5)
    position = result["position"]
    expected_tokens = 0.5 * (1 - config.SIM_FEE_PCT) / (0.001 / SOL_PRICE)
    assert position["token_amount"] == pytest.approx(expected_tokens)
    assert position["entry_price_usd"] == pytest.approx(0.001)
    assert position["tp_price_usd"] == pytest.approx(
        0.001 * (1 + store.settings["take_profit_pct"] / 100))
    assert position["sl_price_usd"] == pytest.approx(
        0.001 * (1 - store.settings["stop_loss_pct"] / 100))
    assert store.stats["sim_balance_sol"] == pytest.approx(0.5)
    assert store.get_position(MINT_A) is not None


async def test_dry_buy_insufficient_paper_balance(engine):
    with pytest.raises(TradeError, match="Insufficient"):
        await engine.buy(MINT_A, 5.0)


async def test_rebuy_merges_and_averages_entry(engine, dex):
    await engine.buy(MINT_A, 0.2)
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.002)  # price doubled
    result = await engine.buy(MINT_A, 0.2)
    position = result["position"]
    assert result["merged"] is True
    assert 0.001 < position["entry_price_usd"] < 0.002
    assert position["sol_spent"] == pytest.approx(0.4)
    assert len(engine.store.positions) == 1


async def test_max_positions_enforced(engine, dex, store):
    store.set_setting("max_positions", 1)
    dex.pairs_by_mint[MINT_B] = make_pair(mint=MINT_B, symbol="TWO")
    await engine.buy(MINT_A, 0.1)
    with pytest.raises(TradeError, match="Max open positions"):
        await engine.buy(MINT_B, 0.1)
    # adding to the existing position is still allowed
    result = await engine.buy(MINT_A, 0.1)
    assert result["merged"] is True


async def test_partial_then_full_sell_closes_with_pnl(engine, dex, store):
    await engine.buy(MINT_A, 0.5)
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.002)  # +100%

    partial = await engine.sell(MINT_A, 50)
    assert partial["closed"] is False
    position = partial["position"]
    assert position["sol_received"] > 0
    tokens_after_partial = position["token_amount"]

    final = await engine.sell(MINT_A, 100)
    assert final["closed"] is True
    trade = final["trade"]
    assert trade["pnl_sol"] > 0
    assert trade["pnl_pct"] > 50  # doubled price minus sim fees
    assert store.get_position(MINT_A) is None
    assert store.summary()["closed_trades"] == 1
    assert store.stats["realized_pnl_sol"] == pytest.approx(trade["pnl_sol"])
    # paper balance grew: 1.0 start, spent 0.5, got back more than 0.5
    assert store.stats["sim_balance_sol"] > 1.0
    assert tokens_after_partial > 0


async def test_sell_without_position_raises(engine):
    with pytest.raises(TradeError, match="No open position"):
        await engine.sell(MINT_A, 100)


async def test_check_exits_take_profit(engine, dex, store):
    await engine.buy(MINT_A, 0.5)
    tp_price = store.get_position(MINT_A)["tp_price_usd"]
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=tp_price * 1.05)
    events = await engine.check_exits()
    assert len(events) == 1
    assert events[0]["type"] == "exit"
    assert events[0]["reason"] == "take_profit"
    assert store.get_position(MINT_A) is None


async def test_check_exits_stop_loss(engine, dex, store):
    await engine.buy(MINT_A, 0.5)
    sl_price = store.get_position(MINT_A)["sl_price_usd"]
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=sl_price * 0.95)
    events = await engine.check_exits()
    assert events[0]["reason"] == "stop_loss"
    assert events[0]["pnl_sol"] < 0


async def test_trailing_stop_follows_peak(engine, dex, store):
    store.set_setting("take_profit_pct", 500.0)  # keep TP out of the way
    store.set_setting("trailing_stop_pct", 10.0)
    await engine.buy(MINT_A, 0.5)

    # price runs up: peak should update, no exit yet
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.002)
    assert await engine.check_exits() == []
    assert store.get_position(MINT_A)["peak_price_usd"] == pytest.approx(0.002)

    # pulls back 15% from the peak -> trailing stop (peak*0.9) fires, in profit
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.0017)
    events = await engine.check_exits()
    assert events[0]["reason"] == "trailing_stop"
    assert events[0]["pnl_sol"] > 0


async def test_panic_sells_everything(engine, dex, store):
    dex.pairs_by_mint[MINT_B] = make_pair(mint=MINT_B, symbol="TWO")
    await engine.buy(MINT_A, 0.2)
    await engine.buy(MINT_B, 0.2)
    results = await engine.panic_sell_all()
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert store.positions == {}
    assert all(t["result"] == "panic" for t in store.data["closed_trades"])


def test_price_in_sol_for_sol_and_usd_quotes():
    sol_quoted = make_pair(price_usd=0.001)
    assert price_in_sol(sol_quoted, SOL_PRICE) == pytest.approx(0.001 / SOL_PRICE)
    usdc_quoted = make_pair(price_usd=0.5, quote_symbol="USDC")
    assert price_in_sol(usdc_quoted, 200.0) == pytest.approx(0.0025)


async def test_buy_rejects_unknown_token(store):
    engine = TradingEngine(store, FakeDex(pairs_by_mint={}), dry_run=True)
    with pytest.raises(TradeError, match="No DexScreener pair"):
        await engine.buy(MINT_A, 0.1)


async def test_mode_mismatch_positions_are_protected(engine, dex, store):
    """A position opened in dry-run must not be sellable by a live engine
    (and vice versa) — and automated exits must skip it, not error on it."""
    await engine.buy(MINT_A, 0.2)
    live_engine = TradingEngine(store, dex, dry_run=False)
    with pytest.raises(TradeError, match="DRY-RUN mode but the bot is now"):
        await live_engine.sell(MINT_A, 100)
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.000001)  # deep below SL
    assert await live_engine.check_exits() == []
    assert store.get_position(MINT_A) is not None
