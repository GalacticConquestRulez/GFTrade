"""Helius integrations: the pre-buy honeypot check (Enhanced Transactions
API) and the RPC failover to a standby endpoint.

The governing rule for the honeypot check is the same asymmetry the rest
of the bot uses: it may PROVE a honeypot (real buys, no successful sells)
and block the trade, but an outage, a thin history, a missing API key or
an unparseable body is UNKNOWN — and unknown never blocks. An unreachable
API is not evidence of fraud, and must never become a way to freeze all
trading."""
import httpx
import pytest

from gftrade import config
from gftrade.clients.helius import (HONEYPOT, SELLS_OK, UNKNOWN,
                                    HeliusEnhanced, analyze_swaps)
from gftrade.solana_rpc import RpcError, SolanaRpc
from gftrade.trading.engine import TradeError, TradingEngine

from conftest import MINT_A, FakeDex, make_pair

WSOL = "So11111111111111111111111111111111111111112"


def swap_tx(token_from=None, token_to=None, sol_to=None, error=None,
            mint=MINT_A, amount=100.0):
    tx = {"type": "SWAP", "transactionError": error,
          "tokenTransfers": [], "nativeTransfers": []}
    if token_from or token_to:
        tx["tokenTransfers"].append({
            "mint": mint, "tokenAmount": amount,
            "fromUserAccount": token_from, "toUserAccount": token_to,
        })
    if sol_to:
        tx["nativeTransfers"].append({"fromUserAccount": "pool",
                                      "toUserAccount": sol_to,
                                      "amount": 500_000})
    return tx


def buy_tx(wallet):
    return swap_tx(token_from="pool", token_to=wallet)


def sell_tx(wallet):
    return swap_tx(token_from=wallet, token_to="pool", sol_to=wallet)


# ---------- the analyzer ----------

def test_real_sells_are_recognized():
    txs = [buy_tx(f"buyer{i}") for i in range(20)]
    txs += [sell_tx("seller1"), sell_tx("seller2")]
    result = analyze_swaps(txs, MINT_A)
    assert result["verdict"] == SELLS_OK
    assert result["sellers"] == 2


def test_many_buys_no_sells_is_the_honeypot_signature():
    result = analyze_swaps([buy_tx(f"buyer{i}") for i in range(20)], MINT_A)
    assert result["verdict"] == HONEYPOT
    assert result["buyers"] == 20 and result["sellers"] == 0


def test_failed_sells_do_not_count_as_sells():
    """A reverted sell is the honeypot symptom — counting it would hide
    exactly what we're looking for."""
    txs = [buy_tx(f"buyer{i}") for i in range(20)]
    txs += [swap_tx(token_from="victim", token_to="pool", sol_to="victim",
                    error={"InstructionError": [3, "custom"]})]
    result = analyze_swaps(txs, MINT_A)
    assert result["verdict"] == HONEYPOT
    assert result["sellers"] == 0


def test_wrapped_sol_proceeds_count_as_a_sell():
    tx = {"type": "SWAP", "transactionError": None, "nativeTransfers": [],
          "tokenTransfers": [
              {"mint": MINT_A, "tokenAmount": 10.0,
               "fromUserAccount": "seller", "toUserAccount": "pool"},
              {"mint": WSOL, "tokenAmount": 0.4,
               "fromUserAccount": "pool", "toUserAccount": "seller"}]}
    txs = [buy_tx(f"b{i}") for i in range(20)] + [tx, sell_tx("seller2")]
    assert analyze_swaps(txs, MINT_A)["verdict"] == SELLS_OK


def test_thin_history_is_unknown_not_an_accusation():
    assert analyze_swaps([buy_tx("b1"), buy_tx("b2")], MINT_A)["verdict"] == UNKNOWN
    assert analyze_swaps([], MINT_A)["verdict"] == UNKNOWN


def test_other_tokens_and_junk_are_ignored():
    noise = [swap_tx(token_from="pool", token_to="someone", mint="OTHERMINT")
             for _ in range(30)]
    assert analyze_swaps(noise, MINT_A)["verdict"] == UNKNOWN
    for junk in (None, {}, "nope", [None, {"tokenTransfers": "bad"}]):
        assert analyze_swaps(junk, MINT_A)["verdict"] == UNKNOWN


# ---------- the client ----------

def fake_helius(handler):
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return HeliusEnhanced(client, api_key="testkey")


async def test_client_parses_and_caches():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=[buy_tx(f"b{i}") for i in range(20)])

    helius = fake_helius(handler)
    assert (await helius.honeypot_check(MINT_A))["verdict"] == HONEYPOT
    await helius.honeypot_check(MINT_A)
    assert len(calls) == 1  # cached: 100 credits spent once, not twice
    assert "type=SWAP" in str(calls[0])


async def test_client_failures_are_unknown_never_exceptions():
    for handler in (
        lambda request: httpx.Response(500, text="boom"),
        lambda request: httpx.Response(200, text="not json"),
        lambda request: httpx.Response(200, json={"unexpected": "shape"}),
    ):
        helius = fake_helius(handler)
        assert (await helius.honeypot_check(MINT_A))["verdict"] == UNKNOWN

    def explode(request):
        raise httpx.ConnectError("network down")

    assert (await fake_helius(explode).honeypot_check(MINT_A))["verdict"] == UNKNOWN


async def test_no_api_key_means_unknown():
    helius = HeliusEnhanced(None, api_key="")
    assert not helius.enabled
    assert (await helius.honeypot_check(MINT_A))["verdict"] == UNKNOWN


# ---------- the engine gate ----------

class StubHelius:
    def __init__(self, verdict, buyers=20, sellers=0):
        self.result = {"verdict": verdict, "buyers": buyers,
                       "sellers": sellers, "examined": buyers + sellers}
        self.calls = 0

    async def honeypot_check(self, mint):
        self.calls += 1
        return self.result


def engine_with(store, helius):
    dex = FakeDex(pairs_by_mint={MINT_A: make_pair()})
    return TradingEngine(store, dex, dry_run=True, helius=helius)


async def test_honeypot_verdict_blocks_the_buy(store):
    helius = StubHelius(HONEYPOT)
    with pytest.raises(TradeError) as exc:
        await engine_with(store, helius).buy(MINT_A, 0.1)
    assert "Honeypot" in str(exc.value)
    assert store.get_position(MINT_A) is None  # no paper position either


async def test_sells_ok_and_unknown_both_allow_the_buy(store):
    for verdict in (SELLS_OK, UNKNOWN):
        store.positions.clear()
        result = await engine_with(store, StubHelius(verdict)).buy(MINT_A, 0.1)
        assert result["position"]["mint"] == MINT_A


async def test_broken_helius_never_blocks_trading(store):
    class BoomHelius:
        async def honeypot_check(self, mint):
            raise ConnectionError("helius down")

    result = await engine_with(store, BoomHelius()).buy(MINT_A, 0.1)
    assert result["position"]["mint"] == MINT_A


async def test_setting_off_skips_the_check_entirely(store):
    store.set_setting("honeypot_check", False)
    helius = StubHelius(HONEYPOT)
    result = await engine_with(store, helius).buy(MINT_A, 0.1)
    assert result["position"]["mint"] == MINT_A
    assert helius.calls == 0  # no credits spent when disabled


async def test_check_is_skipped_when_the_buy_would_fail_anyway(store):
    """Credits are only spent on buys that could actually happen."""
    helius = StubHelius(HONEYPOT)
    with pytest.raises(TradeError):
        await engine_with(store, helius).buy(MINT_A, 0)  # invalid amount
    assert helius.calls == 0


# ---------- RPC failover ----------

def rpc_with(handler, fallback="https://fallback.example/rpc"):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SolanaRpc("https://primary.example/rpc", client, fallback_url=fallback)


async def test_failover_to_standby_endpoint():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "primary" in str(request.url):
            raise httpx.ConnectError("primary down")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}})

    rpc = rpc_with(handler)
    assert await rpc._call("getSomething", []) == {"ok": 1}
    assert any("primary" in url for url in seen)
    assert any("fallback" in url for url in seen)


async def test_repeated_primary_failures_stop_paying_its_timeout():
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if "primary" in str(request.url):
            raise httpx.ConnectError("primary down")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 1})

    rpc = rpc_with(handler)
    for _ in range(config.RPC_FAILOVER_AFTER):
        await rpc._call("m", [])
    seen.clear()
    await rpc._call("m", [])
    assert not any("primary" in url for url in seen)  # skipped while sin-binned


async def test_primary_recovery_resets_the_counter():
    state = {"fail": True}

    def handler(request):
        if "primary" in str(request.url) and state["fail"]:
            raise httpx.ConnectError("primary down")
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 1})

    rpc = rpc_with(handler)
    await rpc._call("m", [])
    assert rpc._primary_failures == 1
    state["fail"] = False
    await rpc._call("m", [])
    assert rpc._primary_failures == 0


async def test_rpc_error_is_an_answer_not_a_failover():
    """A node that replies 'no such account' has answered — trying the
    standby would just repeat the same question."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                                         "error": {"code": -32602, "message": "bad"}})

    with pytest.raises(RpcError):
        await rpc_with(handler)._call("m", [])
    assert len(seen) == 1


# ---------- RPC rate limiting ----------

async def test_limiter_spaces_requests_to_the_configured_rate():
    """The provider's cap is per second at the wire, so the ceiling has to
    live where requests leave — a discovery pass fires ~6 calls per coin
    and that count changes as checks change."""
    import time as _time
    from gftrade.solana_rpc import RateLimiter

    limiter = RateLimiter(rps=50)  # 20ms apart
    started = _time.monotonic()
    for _ in range(6):
        await limiter.acquire()
    elapsed = _time.monotonic() - started
    assert elapsed >= 5 * 0.020 * 0.9  # 5 gaps, allowing scheduler slop


async def test_limiter_queues_concurrent_callers_without_stampeding():
    import asyncio
    import time as _time
    from gftrade.solana_rpc import RateLimiter

    limiter = RateLimiter(rps=50)
    started = _time.monotonic()
    await asyncio.gather(*(limiter.acquire() for _ in range(6)))
    assert _time.monotonic() - started >= 5 * 0.020 * 0.9


async def test_limiter_disabled_when_rps_is_zero():
    from gftrade.solana_rpc import RateLimiter

    limiter = RateLimiter(rps=0)
    for _ in range(50):
        await limiter.acquire()  # returns immediately, no pacing


async def test_rpc_calls_go_through_the_limiter():
    import time as _time

    def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = SolanaRpc("https://primary.example/rpc", client, max_rps=50)
    started = _time.monotonic()
    for _ in range(5):
        await rpc._call("m", [])
    assert _time.monotonic() - started >= 4 * 0.020 * 0.9


async def test_no_fallback_configured_behaves_as_before():
    def handler(request):
        raise httpx.ConnectError("down")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rpc = SolanaRpc("https://primary.example/rpc", client)
    with pytest.raises(httpx.ConnectError):
        await rpc._call("m", [])


# ---------- Gatekeeper auto-pairing ----------

TRACKED = ("SOLANA_RPC_URL", "SOLANA_RPC_FALLBACK_URL", "HELIUS_GATEKEEPER")


def reload_config(**env):
    """Reimport config under a specific environment (it reads os.environ at
    import time) and return a SNAPSHOT of the resulting values.

    A snapshot rather than the module itself because importlib.reload
    mutates the module in place — the cleanup reload below would otherwise
    overwrite the very values under test before any assertion ran."""
    import importlib
    import os
    saved = {k: os.environ.get(k) for k in TRACKED}
    try:
        for key in TRACKED:
            value = env.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import gftrade.config as cfg
        importlib.reload(cfg)
        return {"SOLANA_RPC_URL": cfg.SOLANA_RPC_URL,
                "SOLANA_RPC_FALLBACK_URL": cfg.SOLANA_RPC_FALLBACK_URL,
                "HELIUS_API_KEY": cfg.HELIUS_API_KEY}
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import gftrade.config as cfg
        importlib.reload(cfg)


MAINNET = "https://mainnet.helius-rpc.com/?api-key=testkey123"
GATEKEEPER = "https://beta.helius-rpc.com/?api-key=testkey123"


def test_helius_mainnet_url_auto_pairs_with_gatekeeper():
    """A plain Helius RPC needs no extra config: the faster edge gateway
    becomes the primary and the configured URL becomes the standby, with
    the key carried across rather than duplicated anywhere."""
    cfg = reload_config(SOLANA_RPC_URL=MAINNET)
    assert cfg["SOLANA_RPC_URL"] == GATEKEEPER
    assert cfg["SOLANA_RPC_FALLBACK_URL"] == MAINNET
    assert cfg["HELIUS_API_KEY"] == "testkey123"  # honeypot check still keyed


def test_explicit_fallback_is_never_overridden():
    cfg = reload_config(SOLANA_RPC_URL=GATEKEEPER,
                        SOLANA_RPC_FALLBACK_URL=MAINNET)
    assert cfg["SOLANA_RPC_URL"] == GATEKEEPER
    assert cfg["SOLANA_RPC_FALLBACK_URL"] == MAINNET


def test_gatekeeper_can_be_switched_off():
    cfg = reload_config(SOLANA_RPC_URL=MAINNET, HELIUS_GATEKEEPER="false")
    assert cfg["SOLANA_RPC_URL"] == MAINNET
    assert cfg["SOLANA_RPC_FALLBACK_URL"] == ""


def test_non_helius_rpc_is_left_alone():
    other = "https://my-node.example.com/rpc"
    cfg = reload_config(SOLANA_RPC_URL=other)
    assert cfg["SOLANA_RPC_URL"] == other
    assert cfg["SOLANA_RPC_FALLBACK_URL"] == ""


def test_already_gatekeeper_url_does_not_double_swap():
    cfg = reload_config(SOLANA_RPC_URL=GATEKEEPER)
    assert cfg["SOLANA_RPC_URL"] == GATEKEEPER
    assert "beta.beta" not in cfg["SOLANA_RPC_URL"]
