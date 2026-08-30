"""
Minimal async Solana JSON-RPC client over httpx. Only the handful of
methods this bot needs — read-only account/token queries, transaction
submission, and confirmation polling. Keeping this thin (instead of
depending on solana-py) avoids the historically fragile solana/solders
version matrix; solders alone handles keys and transaction signing.
"""
import asyncio
import base64
import itertools

import httpx


class RpcError(Exception):
    pass


class SolanaRpc:
    def __init__(self, url: str, client: httpx.AsyncClient):
        self.url = url
        self._client = client
        self._ids = itertools.count(1)

    async def _call(self, method: str, params: list):
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params}
        resp = await self._client.post(self.url, json=payload, timeout=15)
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RpcError(f"{method}: {body['error']}")
        return body.get("result")

    # ---------- reads ----------

    async def get_balance_sol(self, pubkey: str) -> float:
        result = await self._call("getBalance", [pubkey])
        return result["value"] / 1_000_000_000

    async def get_mint_info(self, mint: str):
        """Parsed SPL mint account: decimals, supply, and whether the mint /
        freeze authorities still exist (None = renounced, the good case)."""
        result = await self._call(
            "getAccountInfo", [mint, {"encoding": "jsonParsed"}]
        )
        value = (result or {}).get("value")
        if not value:
            return None
        parsed = ((value.get("data") or {}).get("parsed") or {})
        if parsed.get("type") != "mint":
            return None
        info = parsed.get("info") or {}
        return {
            "decimals": info.get("decimals"),
            "supply": int(info.get("supply") or 0),
            "mint_authority": info.get("mintAuthority"),
            "freeze_authority": info.get("freezeAuthority"),
            # which token program owns the mint (classic SPL vs Token-2022)
            "owner_program": value.get("owner"),
        }

    async def get_token_largest_accounts(self, mint: str) -> list:
        """Top ~20 token accounts for a mint: [{address, amount(str raw)}...]."""
        result = await self._call("getTokenLargestAccounts", [mint])
        return (result or {}).get("value") or []

    async def get_account_raw(self, pubkey: str):
        """(owner_program, data_bytes) for any account, or (None, None) when
        it doesn't exist. Used to parse non-token program state (AMM pools)."""
        result = await self._call(
            "getAccountInfo", [pubkey, {"encoding": "base64"}]
        )
        value = (result or {}).get("value")
        if not value:
            return None, None
        data = value.get("data")
        raw = b""
        if isinstance(data, list) and data and isinstance(data[0], str):
            raw = base64.b64decode(data[0])
        return value.get("owner"), raw

    async def get_token_account_owners(self, addresses: list) -> list:
        """The owner wallet of each SPL token account, aligned with the
        input list; None where an entry is missing or not a token account.
        getMultipleAccounts caps at 100 addresses — callers pass fewer."""
        if not addresses:
            return []
        result = await self._call(
            "getMultipleAccounts", [addresses, {"encoding": "jsonParsed"}]
        )
        owners = []
        for value in (result or {}).get("value") or []:
            owner = None
            if isinstance(value, dict):
                parsed = ((value.get("data") or {}).get("parsed") or {})
                if parsed.get("type") == "account":
                    owner = (parsed.get("info") or {}).get("owner")
            owners.append(owner)
        return owners

    async def get_token_balance(self, owner: str, mint: str):
        """Sum of the owner's token accounts for `mint`.
        Returns (raw_amount:int, decimals:int|None, ui_amount:float)."""
        result = await self._call(
            "getTokenAccountsByOwner",
            [owner, {"mint": mint}, {"encoding": "jsonParsed"}],
        )
        raw, decimals = 0, None
        for entry in (result or {}).get("value") or []:
            info = entry["account"]["data"]["parsed"]["info"]
            token_amount = info["tokenAmount"]
            raw += int(token_amount["amount"])
            decimals = token_amount["decimals"]
        ui = raw / (10 ** decimals) if decimals is not None else 0.0
        return raw, decimals, ui

    # ---------- writes ----------

    async def send_raw_transaction_b64(self, signed_tx_b64: str) -> str:
        return await self._call(
            "sendTransaction",
            [signed_tx_b64, {"encoding": "base64", "skipPreflight": False, "maxRetries": 3}],
        )

    async def confirm_transaction(self, signature: str, timeout_seconds: int = 60) -> bool:
        """Poll until the tx is confirmed/finalized without an error.
        Returns False on timeout or on-chain error."""
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        while asyncio.get_event_loop().time() < deadline:
            result = await self._call(
                "getSignatureStatuses", [[signature], {"searchTransactionHistory": True}]
            )
            status = ((result or {}).get("value") or [None])[0]
            if status is not None:
                if status.get("err") is not None:
                    return False
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            await asyncio.sleep(2)
        return False
