"""Shared fixtures: realistic DexScreener pair objects and fake clients so
every test runs offline and deterministic."""
import time

import pytest

from gftrade.discovery.safety import SafetyReport
from gftrade.store import Store
from gftrade.trading.engine import TradingEngine

MINT_A = "A" * 40 + "aaaa"
MINT_B = "B" * 40 + "bbbb"
SOL_PRICE = 200.0


def make_pair(mint=MINT_A, symbol="TEST", price_usd=0.001, price_native=None,
              liquidity=25_000, market_cap=250_000, age_hours=3.0,
              buys_5m=15, sells_5m=7, buys_h1=80, sells_h1=50,
              vol_m5=5_000, vol_h1=30_000, vol_h24=200_000,
              chg_m5=2.0, chg_h1=15.0, chg_h6=20.0, chg_h24=30.0,
              quote_symbol="SOL", boosts_active=0):
    """A pair that passes every hard filter with the default config."""
    if price_native is None:
        price_native = price_usd / SOL_PRICE
    pair = {
        "chainId": "solana",
        "dexId": "raydium",
        "url": f"https://dexscreener.com/solana/pair{mint[:6]}",
        "pairAddress": f"PAIR{mint[:8]}",
        "baseToken": {"address": mint, "name": f"{symbol} Token", "symbol": symbol},
        "quoteToken": {"address": "So11111111111111111111111111111111111111112",
                       "symbol": quote_symbol},
        "priceUsd": str(price_usd),
        "priceNative": str(price_native),
        "txns": {"m5": {"buys": buys_5m, "sells": sells_5m},
                 "h1": {"buys": buys_h1, "sells": sells_h1}},
        "volume": {"m5": vol_m5, "h1": vol_h1, "h24": vol_h24},
        "priceChange": {"m5": chg_m5, "h1": chg_h1, "h6": chg_h6, "h24": chg_h24},
        "liquidity": {"usd": liquidity, "base": 0, "quote": 0},
        "marketCap": market_cap,
        "fdv": market_cap,
        "pairCreatedAt": int((time.time() - age_hours * 3600) * 1000),
    }
    if boosts_active:
        pair["boosts"] = {"active": boosts_active}
    return pair


def make_strong_pair(mint=MINT_A, symbol="MOON", **overrides):
    """A pair that also clears the default alert/autobuy score thresholds."""
    kwargs = dict(
        mint=mint, symbol=symbol, liquidity=60_000, market_cap=400_000,
        buys_5m=25, sells_5m=10, buys_h1=200, sells_h1=120,
        vol_m5=12_000, vol_h1=30_000, vol_h24=200_000,
        chg_m5=4.0, chg_h1=25.0, chg_h6=25.0,
    )
    kwargs.update(overrides)
    return make_pair(**kwargs)


GOOD_SAFETY = SafetyReport(mint=MINT_A, decimals=9, mint_renounced=True,
                           freeze_none=True, top10_pct=10.0, lp_locked_pct=100.0)


class FakeDex:
    def __init__(self, pairs_by_mint=None, profiles=None, boosted=None,
                 sol_price=SOL_PRICE):
        self.pairs_by_mint = pairs_by_mint or {}
        self.profiles = profiles or []
        self.boosted = boosted or set()
        self.sol_price = sol_price

    async def token_profiles_latest(self):
        return self.profiles

    async def boosted_token_addresses(self):
        return self.boosted

    async def pairs_for_tokens(self, chain_id, addresses):
        pairs = []
        for address in addresses:
            pair = self.pairs_by_mint.get(address)
            if pair is not None:
                pairs.append(pair)
        return pairs

    async def pairs_for_token(self, chain_id, address):
        return await self.pairs_for_tokens(chain_id, [address])

    async def best_pair(self, chain_id, mint):
        return self.pairs_by_mint.get(mint)

    async def sol_price_usd(self):
        return self.sol_price


class FakeSafety:
    def __init__(self, report=GOOD_SAFETY):
        self.report = report

    async def check(self, mint):
        report = SafetyReport(**{**self.report.__dict__, "mint": mint})
        return report


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "state.json"))


@pytest.fixture
def dex():
    return FakeDex(pairs_by_mint={MINT_A: make_pair()})


@pytest.fixture
def engine(store, dex):
    return TradingEngine(store, dex, dry_run=True)
