"""GoPlus parsing and client hardening — the backup LP-lock/authority
source. Evidence rules under test: GoPlus can PROVE a lock, but never
proves an unlock (that always needs primary-source/on-chain evidence)."""
import asyncio

import pytest

from gftrade.clients.goplus import GoPlus, parse_authorities, parse_lp_locked_pct


def test_burn_percent_taken_from_highest_tvl_pool_only():
    """A burned dust pool must not vouch for a token whose real liquidity
    is unlocked elsewhere."""
    data = {"dex": [
        {"id": "main", "tvl": "250000", "burn_percent": 55.0},
        {"id": "dust", "tvl": "40", "burn_percent": 100.0},
    ]}
    assert parse_lp_locked_pct(data) == 55.0


def test_multi_pool_without_tvls_gives_no_verdict():
    """Unidentifiable main pool: refusing beats letting list order pick."""
    data = {"dex": [
        {"id": "a", "burn_percent": 100.0},
        {"id": "b", "burn_percent": 0.0},
    ]}
    assert parse_lp_locked_pct(data) is None


def test_single_pool_needs_no_tvl_tiebreak():
    assert parse_lp_locked_pct({"dex": [{"burn_percent": 88.0}]}) == 88.0


def test_zero_burn_is_not_evidence_of_unlocked():
    """burn_percent 0 means 'not burned', not 'not locked' — locker
    contracts aren't burns, so zero yields no verdict."""
    assert parse_lp_locked_pct({"dex": [{"tvl": "9000", "burn_percent": 0.0}]}) is None


def test_stringified_burn_percent_accepted():
    assert parse_lp_locked_pct({"dex": [{"burn_percent": "99.95"}]}) == 99.95


def test_locked_holders_sum_fraction_to_percent():
    data = {"lp_holders": [
        {"percent": "0.97", "tag": "Burn"},
        {"percent": "0.02", "is_locked": 1},
        {"percent": "0.01"},  # neither burned nor locked -> no verdict from it
    ]}
    assert parse_lp_locked_pct(data) == 99.0


def test_out_of_range_fractions_rejected_not_rescaled():
    """percent is documented as a 0-1 fraction; a 97 would silently become
    9700->100 if capped. Scale drift must yield no verdict instead."""
    assert parse_lp_locked_pct({"lp_holders": [{"percent": "97", "tag": "Burn"}]}) is None


def test_combined_signals_take_the_stronger_one():
    data = {
        "dex": [{"tvl": "10000", "burn_percent": 40.0}],
        "lp_holders": [{"percent": "0.95", "is_locked": "1"}],
    }
    assert parse_lp_locked_pct(data) == 95.0


def test_unknown_shapes_stay_unknown():
    assert parse_lp_locked_pct(None) is None
    assert parse_lp_locked_pct({}) is None
    assert parse_lp_locked_pct({"dex": [], "lp_holders": []}) is None
    assert parse_lp_locked_pct({"dex": [{"tvl": "1", "burn_percent": "high"}]}) is None
    assert parse_lp_locked_pct({"lp_holders": [{"percent": "junk", "tag": "Burn"}]}) is None
    # unlocked-looking holders alone are still no verdict (flags optional)
    assert parse_lp_locked_pct({"lp_holders": [{"percent": "0.9"}]}) is None


def test_lp_pct_clamped_to_100():
    data = {"lp_holders": [{"percent": "0.9", "tag": "Burn"},
                           {"percent": "0.4", "is_locked": 1}]}
    assert parse_lp_locked_pct(data) == 100.0


def test_authorities_status_semantics_str_and_int():
    data = {"mintable": {"status": "0", "authority": []},
            "freezable": {"status": "1", "authority": [{"address": "Dev"}]}}
    assert parse_authorities(data) == {"mint_renounced": True, "freeze_none": False}
    # integer status variant must parse identically
    assert parse_authorities({"mintable": {"status": 0}, "freezable": {"status": 1}}) \
        == {"mint_renounced": True, "freeze_none": False}
    assert parse_authorities({"mintable": {"status": "0"}}) \
        == {"mint_renounced": True, "freeze_none": None}
    assert parse_authorities({}) is None
    assert parse_authorities(None) is None
    assert parse_authorities({"mintable": {"status": "weird"}}) is None


class _Resp:
    def __init__(self, status_code=200, body=None, raise_json=False):
        self.status_code = status_code
        self._body = body
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._body


class _Http:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    async def get(self, *args, **kwargs):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def test_security_survives_hostile_envelopes_and_caches_failures():
    mint = "M" * 44
    hostile = [
        _Resp(200, raise_json=True),                       # CDN HTML page
        _Resp(200, {"code": 1, "result": ["not", "a", "dict"]}),
        _Resp(200, {"code": 0, "message": "err", "result": {}}),
        _Resp(500, {}),
        ConnectionError("boom"),
    ]
    for item in hostile:
        http = _Http([item])
        client = GoPlus(http)
        client._last_call_at = 0.0
        assert await client.lp_locked_pct(mint) is None
        # the failure was cached — a second call within TTL never refetches
        assert await client.lp_locked_pct(mint) is None
        assert http.calls == 1
        assert client._cache[mint][0] is None


async def test_security_accepts_string_code_and_serves_cache():
    mint = "M" * 44
    body = {"code": "1", "result": {mint: {"dex": [{"burn_percent": 90.0}]}}}
    http = _Http([_Resp(200, body)])
    client = GoPlus(http)
    client._last_call_at = 0.0
    assert await client.lp_locked_pct(mint) == 90.0
    assert await client.lp_locked_pct(mint) == 90.0  # cached
    assert http.calls == 1
