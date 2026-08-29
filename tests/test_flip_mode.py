"""Flip-mode behavior: unverified-coin alerts, the autobuy ✅-only wall,
and the fast exit cadence's discovery split."""
from gftrade.discovery.safety import SafetyReport
from gftrade.scanner import Scanner
from gftrade.tg import formatting as fmt
from gftrade.trading.engine import TradingEngine

from conftest import MINT_A, FakeDex, FakeSafety, make_strong_pair


UNVERIFIED = SafetyReport(mint=MINT_A, mint_renounced=True, freeze_none=True,
                          top10_pct=10.0, lp_locked_pct=None,
                          standard_token=True)  # ❓ only
KNOWN_BAD = SafetyReport(mint=MINT_A, mint_renounced=True, freeze_none=False,
                         top10_pct=10.0, lp_locked_pct=100.0,
                         standard_token=True)  # honeypot lever


def build(store, report):
    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()},
                  profiles=[{"chainId": "solana", "tokenAddress": MINT_A}])
    engine = TradingEngine(store, dex, dry_run=True)
    return Scanner(store, dex, engine, FakeSafety(report))


async def test_unverified_alerts_only_when_opted_in(store):
    scanner = build(store, UNVERIFIED)
    events = await scanner.tick()
    assert [e for e in events if e["type"] == "signal"] == []  # default: off

    store.set_setting("alert_unverified", True)
    store.data["alerts"] = {}
    store.save()
    events = await scanner.tick()
    signals = [e for e in events if e["type"] == "signal"]
    assert len(signals) == 1
    assert signals[0]["verdict"]["safety_ok"] is False


async def test_known_bad_never_alerts_even_opted_in(store):
    store.set_setting("alert_unverified", True)
    scanner = build(store, KNOWN_BAD)
    events = await scanner.tick()
    assert [e for e in events if e["type"] in ("signal", "autobuy")] == []


async def test_autobuy_never_touches_unverified_coins(store):
    store.set_setting("alert_unverified", True)
    store.set_setting("autobuy", True)
    scanner = build(store, UNVERIFIED)
    events = await scanner.tick()
    # it alerts (manual flip material) but never buys on its own
    assert len([e for e in events if e["type"] == "signal"]) == 1
    assert [e for e in events if e["type"] == "autobuy"] == []
    assert store.positions == {}


def test_unverified_signal_card_is_labeled():
    verdict = {
        "pair": make_strong_pair(), "mint": MINT_A, "score": 72,
        "breakdown": {}, "patterns": [], "safety": UNVERIFIED,
        "safety_ok": False, "screened_ok": True, "reject_reasons": [],
    }
    card = fmt.signal_card(verdict)
    assert "UNVERIFIED" in card and "flip-size" in card
    verdict["safety_ok"] = True
    assert "UNVERIFIED" not in fmt.signal_card(verdict)


async def test_exit_only_tick_skips_discovery(store):
    """The fast cadence checks exits without touching feeds or emitting
    signals — discovery stays on the slow interval."""
    scanner = build(store, SafetyReport(mint=MINT_A, mint_renounced=True,
                                        freeze_none=True, top10_pct=10.0,
                                        lp_locked_pct=100.0, standard_token=True))
    events = await scanner.tick(discover=False)
    assert events == []
    assert scanner.pool == {}          # feeds never consulted
    assert store.signal_log == []
    # a discovery tick then behaves normally
    events = await scanner.tick(discover=True)
    assert len([e for e in events if e["type"] == "signal"]) == 1
