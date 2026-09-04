"""Persistent safety cache: a restart must not throw away vetting work.

The property that matters most here is tri-state fidelity. Every safety
field is True / False / unknown, and the whole evidence model rests on
those staying distinct — a round trip that turned unknown into False would
condemn innocent coins, and one that turned unknown into True would let
autobuy fire on data nobody ever verified."""
import time

import pytest

from gftrade.discovery.safety import SafetyChecker, SafetyReport
from gftrade.safety_cache import SafetyCacheStore

from test_safety import FakeRugCheck, clean_rpc

MINT = "S" * 40 + "ssss"


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "safety_cache.db")


def test_round_trip_preserves_every_field(store_path):
    store = SafetyCacheStore(store_path)
    report = SafetyReport(mint=MINT, decimals=9, mint_renounced=True,
                          freeze_none=False, top10_pct=12.5,
                          lp_locked_pct=97.5, lp_source="onchain",
                          standard_token=True, error=None)
    store.put(report, time.time(), 600)

    loaded = SafetyCacheStore(store_path).load()
    assert MINT in loaded
    back = loaded[MINT][0]
    assert (back.mint, back.decimals, back.mint_renounced, back.freeze_none,
            back.top10_pct, back.lp_locked_pct, back.lp_source,
            back.standard_token) == \
           (MINT, 9, True, False, 12.5, 97.5, "onchain", True)


def test_unknown_stays_unknown_not_false(store_path):
    """The critical one: None must survive as None. Collapsing it to
    False would turn an unverified coin into a condemned one; collapsing
    it to True would let autobuy trade on data nobody verified."""
    store = SafetyCacheStore(store_path)
    store.put(SafetyReport(mint=MINT), time.time(), 120)

    back = SafetyCacheStore(store_path).load()[MINT][0]
    assert back.mint_renounced is None
    assert back.freeze_none is None
    assert back.standard_token is None
    assert back.top10_pct is None
    assert back.lp_locked_pct is None
    assert not back.passes(strict=True)   # can never autobuy
    assert back.passes(strict=False)      # but is not condemned either


def test_false_stays_false(store_path):
    store = SafetyCacheStore(store_path)
    store.put(SafetyReport(mint=MINT, mint_renounced=False, freeze_none=False,
                           standard_token=False), time.time(), 600)
    back = SafetyCacheStore(store_path).load()[MINT][0]
    assert back.mint_renounced is False
    assert back.standard_token is False
    assert not back.passes(strict=False)  # still known-bad after a restart


def test_expired_rows_are_never_revived(store_path):
    store = SafetyCacheStore(store_path)
    store.put(SafetyReport(mint=MINT, mint_renounced=True),
              time.time() - 500, 120)  # written 500s ago, 120s TTL
    assert SafetyCacheStore(store_path).load() == {}


def test_prune_drops_only_expired_rows(store_path):
    store = SafetyCacheStore(store_path)
    store.put(SafetyReport(mint="fresh"), time.time(), 600)
    store.put(SafetyReport(mint="stale"), time.time() - 500, 120)
    assert store.prune() == 1
    assert set(SafetyCacheStore(store_path).load()) == {"fresh"}


def test_replacing_a_mint_keeps_one_row(store_path):
    store = SafetyCacheStore(store_path)
    store.put(SafetyReport(mint=MINT, top10_pct=10.0), time.time(), 600)
    store.put(SafetyReport(mint=MINT, top10_pct=20.0), time.time(), 600)
    loaded = SafetyCacheStore(store_path).load()
    assert len(loaded) == 1
    assert loaded[MINT][0].top10_pct == 20.0


# ---------- integration with the checker ----------

async def test_checker_writes_through_and_warm_starts(store_path):
    rpc = clean_rpc()
    checker = SafetyChecker(rpc, FakeRugCheck(100.0),
                            cache_store=SafetyCacheStore(store_path))
    checker.MIN_CHECK_INTERVAL = 0.0
    first = await checker.check(MINT)
    assert first.passes(strict=True)
    assert rpc.mint_info_calls == 1

    # A "restart": brand new checker, same database.
    rpc2 = clean_rpc()
    restarted = SafetyChecker(rpc2, FakeRugCheck(100.0),
                              cache_store=SafetyCacheStore(store_path))
    restarted.MIN_CHECK_INTERVAL = 0.0
    assert restarted.cached(MINT) is not None      # warm before any call
    again = await restarted.check(MINT)
    assert again.passes(strict=True)
    assert rpc2.mint_info_calls == 0               # nothing re-fetched


async def test_a_broken_cache_file_does_not_stop_the_bot(tmp_path):
    """Cache persistence is an optimisation; if it fails the bot must
    still vet coins, just without the head start."""
    bad = tmp_path / "not-a-db.db"
    bad.write_bytes(b"this is not sqlite")
    try:
        store = SafetyCacheStore(str(bad))
    except Exception:
        return  # refusing to open a corrupt file is acceptable too
    assert store.load() == {}
    checker = SafetyChecker(clean_rpc(), FakeRugCheck(100.0), cache_store=store)
    checker.MIN_CHECK_INTERVAL = 0.0
    assert (await checker.check(MINT)).passes(strict=True)


async def test_no_store_configured_still_works(store_path):
    checker = SafetyChecker(clean_rpc(), FakeRugCheck(100.0))
    checker.MIN_CHECK_INTERVAL = 0.0
    assert (await checker.check(MINT)).passes(strict=True)
