"""JSON-RPC array batching — what turns a 140-coin sweep from ~840 HTTP
requests into roughly a dozen.

The two traps this pins down are both silent-corruption class:
responses may come back in any order (so they must be matched by id, never
by position), and one bad entry must not blank the whole batch. A missing
answer always surfaces as None, which callers read as "unknown" — the safe
direction everywhere in this codebase."""
import base64
import json

import httpx
import pytest

from gftrade import config
from gftrade.solana_rpc import RateLimiter, SolanaRpc

MINTS = [f"M{i:02d}" + "z" * 38 for i in range(5)]


def mint_value(supply="1000", authority=None, owner="TokenProg"):
    return {"owner": owner,
            "data": {"parsed": {"type": "mint",
                                "info": {"decimals": 9, "supply": supply,
                                         "mintAuthority": authority,
                                         "freezeAuthority": None}}}}


def build_rpc(handler, max_rps=0):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SolanaRpc("https://primary.example/rpc", client, max_rps=max_rps)


def batch_handler(reply_for, shuffle=False):
    """MockTransport handler that answers a JSON-RPC array by calling
    `reply_for(entry)` for each request entry."""
    seen = {"requests": 0, "sizes": []}

    def handler(request):
        payload = json.loads(request.content)
        seen["requests"] += 1
        if isinstance(payload, dict):
            return httpx.Response(200, json={"jsonrpc": "2.0",
                                             "id": payload["id"],
                                             "result": reply_for(payload)})
        seen["sizes"].append(len(payload))
        out = [{"jsonrpc": "2.0", "id": e["id"], "result": reply_for(e)}
               for e in payload]
        if shuffle:
            out.reverse()  # servers may reorder; we must not care
        return httpx.Response(200, json=out)

    handler.seen = seen
    return handler


# ---------- ordering and error isolation ----------

async def test_batch_results_align_with_input():
    handler = batch_handler(lambda e: {"echo": e["params"][0]})
    rpc = build_rpc(handler)
    calls = [{"method": "getX", "params": [m]} for m in MINTS]
    results = await rpc._call_batch(calls)
    assert [r["echo"] for r in results] == MINTS


async def test_reordered_responses_are_matched_by_id_not_position():
    """A server that returns entries out of order must not cause one
    mint's answer to be attributed to another."""
    handler = batch_handler(lambda e: {"echo": e["params"][0]}, shuffle=True)
    rpc = build_rpc(handler)
    calls = [{"method": "getX", "params": [m]} for m in MINTS]
    results = await rpc._call_batch(calls)
    assert [r["echo"] for r in results] == MINTS  # still aligned


async def test_per_entry_error_is_isolated_to_that_entry():
    def handler(request):
        payload = json.loads(request.content)
        out = []
        for entry in payload:
            if entry["params"][0] == MINTS[2]:
                out.append({"jsonrpc": "2.0", "id": entry["id"],
                            "error": {"code": -32602, "message": "bad mint"}})
            else:
                out.append({"jsonrpc": "2.0", "id": entry["id"],
                            "result": {"echo": entry["params"][0]}})
        return httpx.Response(200, json=out)

    rpc = build_rpc(handler)
    results = await rpc._call_batch(
        [{"method": "getX", "params": [m]} for m in MINTS])
    assert results[2] is None                      # only the poison entry
    assert results[0]["echo"] == MINTS[0]          # neighbours unaffected
    assert results[4]["echo"] == MINTS[4]


async def test_whole_batch_failure_yields_unknowns_not_an_exception():
    def handler(request):
        raise httpx.ConnectError("rpc down")

    rpc = build_rpc(handler)
    results = await rpc._call_batch(
        [{"method": "getX", "params": [m]} for m in MINTS])
    assert results == [None] * len(MINTS)  # unknown, never a crash


async def test_non_array_response_is_survived():
    def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "result": "not an array"})

    rpc = build_rpc(handler)
    assert await rpc._call_batch(
        [{"method": "getX", "params": [m]} for m in MINTS]) == [None] * 5


async def test_unknown_id_in_response_is_ignored():
    def handler(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": 999999, "result": {"echo": "stray"}},
            {"jsonrpc": "2.0", "id": payload[0]["id"],
             "result": {"echo": "real"}},
        ])

    rpc = build_rpc(handler)
    results = await rpc._call_batch([{"method": "getX", "params": ["a"]},
                                     {"method": "getX", "params": ["b"]}])
    assert results[0]["echo"] == "real"
    assert results[1] is None  # never filled from the stray entry


async def test_batch_is_chunked_to_the_configured_size(monkeypatch):
    monkeypatch.setattr(config, "RPC_BATCH_SIZE", 2)
    handler = batch_handler(lambda e: {"ok": True})
    rpc = build_rpc(handler)
    results = await rpc._call_batch(
        [{"method": "getX", "params": [m]} for m in MINTS])
    assert all(r == {"ok": True} for r in results)
    assert handler.seen["sizes"] == [2, 2, 1]


async def test_empty_batch_makes_no_request():
    handler = batch_handler(lambda e: {})
    rpc = build_rpc(handler)
    assert await rpc._call_batch([]) == []
    assert handler.seen["requests"] == 0


# ---------- rate-limit accounting ----------

async def test_batch_costs_one_slot_by_default(monkeypatch):
    """Providers generally count an array as one request; the setting
    exists because some do not."""
    monkeypatch.setattr(config, "RPC_BATCH_COUNTS_AS", "1")
    import time as _time
    handler = batch_handler(lambda e: {"ok": True})
    rpc = build_rpc(handler, max_rps=50)  # 20ms per slot
    started = _time.monotonic()
    await rpc._call_batch([{"method": "getX", "params": [m]} for m in MINTS])
    assert _time.monotonic() - started < 5 * 0.020


async def test_batch_can_be_charged_per_entry(monkeypatch):
    """With "size", a 5-call batch reserves 5 slots. The reservation is
    paid by whatever comes NEXT (the limiter never delays the first
    caller), so the cost is measured across a following call."""
    import time as _time
    handler = batch_handler(lambda e: {"ok": True})
    calls = [{"method": "getX", "params": [m]} for m in MINTS]

    monkeypatch.setattr(config, "RPC_BATCH_COUNTS_AS", "1")
    rpc = build_rpc(handler, max_rps=50)  # 20ms per slot
    started = _time.monotonic()
    await rpc._call_batch(calls)
    await rpc._call_batch(calls)
    cheap = _time.monotonic() - started

    monkeypatch.setattr(config, "RPC_BATCH_COUNTS_AS", "size")
    rpc = build_rpc(handler, max_rps=50)
    started = _time.monotonic()
    await rpc._call_batch(calls)
    await rpc._call_batch(calls)
    charged = _time.monotonic() - started

    assert charged >= 5 * 0.020 * 0.9   # the first batch's 5 slots
    assert charged > cheap              # and strictly more than one slot


async def test_limiter_slots_scale_the_reservation():
    import time as _time

    limiter = RateLimiter(rps=50)
    started = _time.monotonic()
    await limiter.acquire(1)   # first is free
    await limiter.acquire(5)   # reserves 5 x 20ms
    await limiter.acquire(1)
    assert _time.monotonic() - started >= 5 * 0.020 * 0.9


# ---------- batched read helpers ----------

async def test_get_mint_infos_parses_like_the_single_reader():
    def handler(request):
        payload = json.loads(request.content)
        entries = payload if isinstance(payload, list) else [payload]
        out = []
        for entry in entries:
            addresses = entry["params"][0]
            out.append({"jsonrpc": "2.0", "id": entry["id"], "result": {
                "value": [mint_value(supply=str(1000 + i))
                          for i, _ in enumerate(addresses)]}})
        return httpx.Response(200, json=out if isinstance(payload, list) else out[0])

    rpc = build_rpc(handler)
    infos = await rpc.get_mint_infos(MINTS)
    assert set(infos) == set(MINTS)
    assert infos[MINTS[0]]["supply"] == 1000
    assert infos[MINTS[0]]["mint_authority"] is None


async def test_short_response_keeps_alignment():
    """If the node returns fewer values than addresses, the extras must
    map to None rather than shifting every later mint's answer."""
    def handler(request):
        payload = json.loads(request.content)
        entry = payload[0] if isinstance(payload, list) else payload
        return httpx.Response(200, json=[{
            "jsonrpc": "2.0", "id": entry["id"],
            "result": {"value": [mint_value()]},  # 1 value for 5 addresses
        }])

    rpc = build_rpc(handler)
    infos = await rpc.get_mint_infos(MINTS)
    assert infos[MINTS[0]] is not None
    assert all(infos[m] is None for m in MINTS[1:])


async def test_get_account_raws_decodes_base64():
    blob = base64.b64encode(b"\x01\x02\x03").decode()

    def handler(request):
        payload = json.loads(request.content)
        entry = payload[0] if isinstance(payload, list) else payload
        return httpx.Response(200, json=[{
            "jsonrpc": "2.0", "id": entry["id"],
            "result": {"value": [{"owner": "Prog", "data": [blob, "base64"]}]},
        }])

    rpc = build_rpc(handler)
    raws = await rpc.get_account_raws(["addr1"])
    assert raws["addr1"] == ("Prog", b"\x01\x02\x03")


async def test_get_token_largest_accounts_many():
    def handler(request):
        payload = json.loads(request.content)
        return httpx.Response(200, json=[
            {"jsonrpc": "2.0", "id": e["id"],
             "result": {"value": [{"address": f"acc-{e['params'][0]}",
                                   "amount": "500"}]}}
            for e in payload])

    rpc = build_rpc(handler)
    holders = await rpc.get_token_largest_accounts_many(MINTS[:3])
    assert holders[MINTS[0]][0]["address"] == f"acc-{MINTS[0]}"
    assert len(holders) == 3


async def test_batched_helpers_tolerate_missing_mints():
    def handler(request):
        raise httpx.ConnectError("down")

    rpc = build_rpc(handler)
    assert await rpc.get_mint_infos(MINTS) == {m: None for m in MINTS}
    assert await rpc.get_token_largest_accounts_many(MINTS) == {m: [] for m in MINTS}
