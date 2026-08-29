"""The signal report card and the GeckoTerminal early-discovery feed."""
import time

import pytest

from gftrade.clients.geckoterminal import parse_new_pool_mints
from gftrade.scanner import Scanner
from gftrade.tg import formatting as fmt
from gftrade.trading.engine import TradingEngine

from conftest import MINT_A, MINT_B, FakeDex, FakeSafety, make_pair, make_strong_pair


class FakeGecko:
    def __init__(self, mints):
        self.mints = mints

    async def new_solana_pool_mints(self):
        return self.mints


def test_parse_new_pool_mints_defensively():
    data = {"data": [
        {"relationships": {"base_token": {"data": {"id": f"solana_{MINT_A}"}}}},
        {"relationships": {"base_token": {"data": {"id": f"solana_{MINT_A}"}}}},  # dupe
        {"relationships": {"base_token": {"data": {"id": "no-underscore"}}}},
        {"relationships": {"base_token": {"data": {"id": "solana_short"}}}},
        {"relationships": {}},
        "junk",
    ]}
    assert parse_new_pool_mints(data) == [MINT_A]
    assert parse_new_pool_mints({}) == []
    assert parse_new_pool_mints(None) == []


async def test_gecko_feed_populates_pool_and_signals(store):
    """A mint arriving only via the new-pools feed (no DexScreener profile)
    must flow through the full pipeline to a signal."""
    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()}, profiles=[])
    engine = TradingEngine(store, dex, dry_run=True)
    scanner = Scanner(store, dex, engine, FakeSafety(), gecko=FakeGecko([MINT_A]))
    events = await scanner.tick()
    assert MINT_A in scanner.pool or store.recently_alerted(MINT_A)
    assert [e["verdict"]["mint"] for e in events if e["type"] == "signal"] == [MINT_A]


async def test_broken_gecko_feed_does_not_break_discovery(store):
    class BrokenGecko:
        async def new_solana_pool_mints(self):
            raise ConnectionError("gecko down")

    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()},
                  profiles=[{"chainId": "solana", "tokenAddress": MINT_A}])
    engine = TradingEngine(store, dex, dry_run=True)
    scanner = Scanner(store, dex, engine, FakeSafety(), gecko=BrokenGecko())
    events = await scanner.tick()
    assert len([e for e in events if e["type"] == "signal"]) == 1  # profiles still work


async def test_signal_recorded_then_checkpoints_filled(store):
    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()},
                  profiles=[{"chainId": "solana", "tokenAddress": MINT_A}])
    engine = TradingEngine(store, dex, dry_run=True)
    scanner = Scanner(store, dex, engine, FakeSafety())
    await scanner.tick()

    assert len(store.signal_log) == 1
    entry = store.signal_log[0]
    assert entry["mint"] == MINT_A
    assert entry["pattern"] == "accumulation_momentum"
    assert entry["price0"] == pytest.approx(0.001)
    assert entry["h1"] is None

    # 7 hours pass; price has doubled -> h1 and h6 fill, h24 not yet due
    entry["ts"] -= 7 * 3600
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.002)
    await scanner.tick()
    entry = store.signal_log[0]
    assert entry["h1"] == pytest.approx(0.002)
    assert entry["h6"] == pytest.approx(0.002)
    assert entry["h24"] is None


async def test_vanished_token_recorded_as_total_loss(store):
    dex = FakeDex(pairs_by_mint={}, profiles=[])
    engine = TradingEngine(store, dex, dry_run=True)
    scanner = Scanner(store, dex, engine, FakeSafety())
    store.add_signal({"mint": MINT_B, "symbol": "GONE", "pattern": "volume_surge",
                      "score": 80, "price0": 0.001, "ts": time.time() - 4 * 3600,
                      "h1": None, "h6": None, "h24": None})
    await scanner._update_signal_log()
    entry = store.signal_log[0]
    assert entry["h1"] == 0.0   # overdue past the grace period, no market left
    assert entry["h6"] is None  # not due yet at 4h


def test_signal_report_lines_math():
    log = [
        {"pattern": "volume_surge", "price0": 1.0, "h1": 1.5, "h24": 0.5},
        {"pattern": "volume_surge", "price0": 1.0, "h1": 0.8, "h24": 3.0},
        {"pattern": "accumulation_momentum", "price0": 1.0, "h1": None, "h24": None},
    ]
    lines = fmt.signal_report_lines(log)
    text = "\n".join(lines)
    assert "last 3 signals" in text
    assert "volume_surge (2)" in text
    assert "1h: 50% up" in text          # one +50%, one -20%
    assert "24h: 50% up" in text         # one -50%, one +200%
    assert "med +75%" in text            # median of -50 and +200
    assert "collecting data" in text     # the unfilled pattern
    assert fmt.signal_report_lines([]) == []


def test_trades_text_includes_report_card():
    summary = {"closed_trades": 0, "win_rate": None, "realized_pnl_sol": 0.0,
               "sim_balance_sol": 1.0, "open_positions": 0}
    log = [{"pattern": "volume_surge", "price0": 1.0, "h1": 2.0, "h24": None}]
    text = fmt.trades_text(summary, [], True, log)
    assert "Signal report card" in text
    assert "volume_surge" in text
    # without a log the section is absent
    assert "Signal report card" not in fmt.trades_text(summary, [], True)
