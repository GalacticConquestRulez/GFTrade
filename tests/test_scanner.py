from gftrade.scanner import Scanner
from gftrade.trading.engine import TradingEngine

from conftest import MINT_A, MINT_B, FakeDex, FakeSafety, make_pair, make_strong_pair


def build_scanner(store, pairs_by_mint, profiles=None):
    dex = FakeDex(
        pairs_by_mint=pairs_by_mint,
        profiles=profiles if profiles is not None else [
            {"chainId": "solana", "tokenAddress": mint} for mint in pairs_by_mint
        ],
    )
    engine = TradingEngine(store, dex, dry_run=True)
    scanner = Scanner(store, dex, engine, FakeSafety())
    return scanner, dex, engine


async def test_signal_emitted_once_then_cooldown(store):
    scanner, _, _ = build_scanner(store, {MINT_A: make_strong_pair()})
    events = await scanner.tick()
    signals = [e for e in events if e["type"] == "signal"]
    assert len(signals) == 1
    assert signals[0]["verdict"]["mint"] == MINT_A
    assert signals[0]["verdict"]["score"] >= store.settings["min_alert_score"]

    # same tick again: cooldown suppresses a duplicate alert
    events = await scanner.tick()
    assert [e for e in events if e["type"] == "signal"] == []


async def test_weak_pair_produces_no_signal(store):
    weak = make_pair(chg_m5=-1, chg_h1=-5, buys_5m=6, sells_5m=6, vol_m5=600)
    scanner, _, _ = build_scanner(store, {MINT_A: weak})
    events = await scanner.tick()
    assert [e for e in events if e["type"] == "signal"] == []
    # ...but the mint stays in the pool for re-checks
    assert MINT_A in scanner.pool


async def test_muted_mint_never_alerts(store):
    store.mute(MINT_A)
    scanner, _, _ = build_scanner(store, {MINT_A: make_strong_pair()})
    events = await scanner.tick()
    assert [e for e in events if e["type"] == "signal"] == []


async def test_scanner_off_skips_discovery(store):
    store.set_setting("scanner_on", False)
    scanner, _, _ = build_scanner(store, {MINT_A: make_strong_pair()})
    events = await scanner.tick()
    assert events == []
    assert scanner.pool == {}


async def test_autobuy_opens_position(store):
    store.set_setting("autobuy", True)
    scanner, _, engine = build_scanner(store, {MINT_A: make_strong_pair()})
    events = await scanner.tick()
    autobuys = [e for e in events if e["type"] == "autobuy"]
    assert len(autobuys) == 1
    position = store.get_position(MINT_A)
    assert position is not None
    assert position["source"] == "auto"
    assert position["sol_spent"] == store.settings["autobuy_sol"]
    # no duplicate signal for the same token
    assert [e for e in events if e["type"] == "signal"] == []


async def test_autobuy_below_threshold_falls_back_to_signal(store):
    store.set_setting("autobuy", True)
    store.set_setting("min_autobuy_score", 100)  # unreachable
    scanner, _, _ = build_scanner(store, {MINT_A: make_strong_pair()})
    events = await scanner.tick()
    assert [e for e in events if e["type"] == "autobuy"] == []
    assert len([e for e in events if e["type"] == "signal"]) == 1


async def test_boosted_token_rejected(store):
    boosted_pair = make_strong_pair()
    boosted_pair["boosts"] = {"active": 3}
    scanner, _, _ = build_scanner(store, {MINT_A: boosted_pair})
    events = await scanner.tick()
    assert [e for e in events if e["type"] == "signal"] == []


async def test_aged_out_pair_leaves_pool(store):
    old = make_pair(age_hours=100)
    scanner, _, _ = build_scanner(store, {MINT_A: old})
    await scanner.tick()
    assert MINT_A not in scanner.pool


async def test_exit_events_flow_through_tick(store):
    scanner, dex, engine = build_scanner(store, {MINT_A: make_pair()})
    await engine.buy(MINT_A, 0.2)
    tp = store.get_position(MINT_A)["tp_price_usd"]
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=tp * 1.1)
    events = await scanner.tick()
    exits = [e for e in events if e["type"] == "exit"]
    assert len(exits) == 1 and exits[0]["reason"] == "take_profit"


async def test_scan_now_returns_ranked_verdicts(store):
    scanner, _, _ = build_scanner(store, {
        MINT_A: make_strong_pair(),
        MINT_B: make_pair(mint=MINT_B, symbol="MEH"),
    })
    verdicts = await scanner.scan_now(top_n=5)
    assert len(verdicts) == 2
    assert verdicts[0]["score"] >= verdicts[1]["score"]
    assert verdicts[0]["mint"] == MINT_A


async def test_pool_capped(store):
    profiles = [{"chainId": "solana", "tokenAddress": f"POOL{i:040d}"} for i in range(600)]
    scanner, _, _ = build_scanner(store, {}, profiles=profiles)
    scanner._absorb_profiles(profiles)
    from gftrade import config
    assert len(scanner.pool) <= config.CANDIDATE_POOL_MAX
