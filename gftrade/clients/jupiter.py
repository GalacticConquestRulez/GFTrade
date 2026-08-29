"""
Jupiter swap API client: quote -> build transaction -> sign (solders) ->
send via our RPC client -> confirm.

Exits here are bot-monitored (the scanner watches prices and market-sells
when TP/SL/trailing levels are crossed) rather than Jupiter Trigger
("limit") orders. Trigger orders are on-chain and survive the bot dying,
but the API contract has changed shape repeatedly, and outstanding trigger
orders fight with Trojan-style button sells (every partial sell would need
order cancels/re-creates). Monitored exits keep one code path that behaves
identically in dry-run and live. The trade-off: if the bot is down, exits
don't fire — run it somewhere reliable, or close positions before stopping
it. See README "Exit handling" for the full reasoning.
"""
import base64

import httpx
from solders.transaction import VersionedTransaction

from .. import config


class SwapError(Exception):
    pass


class Jupiter:
    def __init__(self, client: httpx.AsyncClient, api_base: str = None, api_key: str = None):
        self._client = client
        self.api_base = api_base or config.JUPITER_API_BASE
        self.api_key = api_key if api_key is not None else config.JUPITER_API_KEY

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["x-api-key"] = self.api_key
        return headers

    async def quote(self, input_mint: str, output_mint: str, amount_raw: int,
                    slippage_bps: int) -> dict:
        """`amount_raw` is in the input token's base units (lamports for SOL)."""
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": amount_raw,
            "slippageBps": slippage_bps,
        }
        resp = await self._client.get(f"{self.api_base}/swap/v1/quote",
                                      params=params, headers=self._headers(), timeout=15)
        if resp.status_code != 200:
            raise SwapError(f"quote failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()

    async def swap_transaction(self, quote_response: dict, user_pubkey: str) -> str:
        """Returns the unsigned swap as a base64 versioned transaction."""
        body = {
            "quoteResponse": quote_response,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": config.PRIORITY_FEE,
        }
        resp = await self._client.post(f"{self.api_base}/swap/v1/swap",
                                       json=body, headers=self._headers(), timeout=20)
        if resp.status_code != 200:
            raise SwapError(f"swap build failed ({resp.status_code}): {resp.text[:300]}")
        return resp.json()["swapTransaction"]

    @staticmethod
    def sign(swap_tx_b64: str, keypair) -> str:
        """Sign the transaction Jupiter built and return it re-encoded."""
        raw = base64.b64decode(swap_tx_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [keypair])
        return base64.b64encode(bytes(signed)).decode()

    async def execute_swap(self, rpc, keypair, input_mint: str, output_mint: str,
                           amount_raw: int, slippage_bps: int) -> dict:
        """Full flow. Returns {"signature", "quote", "confirmed"}. Raises
        SwapError if the transaction can't be built or sent; a sent-but-
        unconfirmed transaction comes back with confirmed=False so the
        caller can tell the user exactly what's in limbo."""
        quote = await self.quote(input_mint, output_mint, amount_raw, slippage_bps)
        tx_b64 = await self.swap_transaction(quote, str(keypair.pubkey()))
        signed_b64 = self.sign(tx_b64, keypair)
        signature = await rpc.send_raw_transaction_b64(signed_b64)
        confirmed = await rpc.confirm_transaction(signature)
        return {"signature": signature, "quote": quote, "confirmed": confirmed}
