"""
Helius Enhanced Transactions API — human-readable swap history, used for
the one question aggregate market data cannot answer honestly:

    has anyone actually SOLD this token, successfully, on-chain?

DexScreener's txns counts show "buys" and "sells", but those are derived
counts; a honeypot's sell attempts fail, and a token can show volume
while every exit reverts. Reading real parsed swaps tells us whether
wallets other than the deployer got their SOL back out.

Endpoint: GET {base}/v0/addresses/{mint}/transactions?api-key=..&type=SWAP
Cost: 100 credits per call (a normal RPC call is 1), so this is NOT a
screening step — the engine calls it once per mint, cached, immediately
before money moves.

Evidence rules match the rest of the bot's asymmetry: this can prove a
token is a honeypot (many buys, zero successful sells) but never proves
one is safe, and any failure/thin history/unparseable body is UNKNOWN,
which never blocks a trade. Being unable to reach an API is not evidence
of fraud — and it must not become a way to freeze all trading.
"""
import logging
import time

from .. import config, constants

logger = logging.getLogger(__name__)

# verdicts
HONEYPOT = "honeypot"      # positive evidence: buys happened, sells didn't
SELLS_OK = "sells_ok"      # real wallets sold successfully
UNKNOWN = "unknown"        # no usable answer — never blocks anything

WSOL = constants.SOL_MINT


def _wallets_moving(tx: dict, mint: str) -> tuple:
    """(senders, receivers) of `mint` in one parsed transaction."""
    senders, receivers = set(), set()
    for transfer in tx.get("tokenTransfers") or []:
        if not isinstance(transfer, dict) or transfer.get("mint") != mint:
            continue
        try:
            amount = float(transfer.get("tokenAmount") or 0)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        if transfer.get("fromUserAccount"):
            senders.add(transfer["fromUserAccount"])
        if transfer.get("toUserAccount"):
            receivers.add(transfer["toUserAccount"])
    return senders, receivers


def _sol_receivers(tx: dict) -> set:
    """Wallets that received SOL (native or wrapped) in this transaction —
    the money side of a sell."""
    out = set()
    for transfer in tx.get("nativeTransfers") or []:
        if isinstance(transfer, dict) and transfer.get("toUserAccount"):
            try:
                if float(transfer.get("amount") or 0) > 0:
                    out.add(transfer["toUserAccount"])
            except (TypeError, ValueError):
                continue
    for transfer in tx.get("tokenTransfers") or []:
        if (isinstance(transfer, dict) and transfer.get("mint") == WSOL
                and transfer.get("toUserAccount")):
            try:
                if float(transfer.get("tokenAmount") or 0) > 0:
                    out.add(transfer["toUserAccount"])
            except (TypeError, ValueError):
                continue
    return out


def analyze_swaps(transactions, mint: str) -> dict:
    """{verdict, sellers, buyers, examined} from parsed Helius transactions.

    A SELL is a wallet sending `mint` away AND receiving SOL in the same
    successful transaction — the round trip a honeypot victim can't make.
    A BUY is the mirror image. Failed transactions are skipped: a reverted
    sell is a honeypot symptom, but counting it as a sell would hide one."""
    if not isinstance(transactions, list):
        return {"verdict": UNKNOWN, "sellers": 0, "buyers": 0, "examined": 0}

    sellers, buyers, examined = set(), set(), 0
    for tx in transactions:
        if not isinstance(tx, dict) or tx.get("transactionError"):
            continue
        senders, receivers = _wallets_moving(tx, mint)
        if not senders and not receivers:
            continue
        examined += 1
        paid_in_sol = _sol_receivers(tx)
        sellers |= (senders & paid_in_sol)     # gave tokens, got SOL
        buyers |= (receivers - paid_in_sol)    # got tokens, wasn't paid SOL

    result = {"sellers": len(sellers), "buyers": len(buyers), "examined": examined}
    if len(sellers) >= config.HONEYPOT_MIN_SELLERS:
        result["verdict"] = SELLS_OK
    elif len(buyers) >= config.HONEYPOT_MIN_BUYS_FOR_VERDICT:
        # Plenty of people got in, (almost) nobody got out — the signature.
        result["verdict"] = HONEYPOT
    else:
        result["verdict"] = UNKNOWN  # too little history to accuse anyone
    return result


class HeliusEnhanced:
    def __init__(self, client, api_key: str = None, api_base: str = None):
        self._client = client
        self.api_key = api_key if api_key is not None else config.HELIUS_API_KEY
        self.api_base = api_base or config.HELIUS_API_BASE
        self._cache = {}  # mint -> (result, fetched_at)

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def swap_history(self, mint: str):
        """Recent parsed SWAP transactions for a mint, or None on any
        failure. Never raises — a dead API must not stop trading."""
        try:
            resp = await self._client.get(
                f"{self.api_base}/v0/addresses/{mint}/transactions",
                params={"api-key": self.api_key, "type": "SWAP",
                        "limit": config.HONEYPOT_TX_LOOKBACK},
                timeout=10,
            )
            if resp.status_code != 200:
                logger.warning("helius enhanced tx %s for %s",
                               resp.status_code, mint)
                return None
            body = resp.json()
            return body if isinstance(body, list) else None
        except Exception:
            logger.warning("helius enhanced tx call failed for %s", mint,
                           exc_info=True)
            return None

    async def honeypot_check(self, mint: str) -> dict:
        """{verdict, sellers, buyers, examined} for a mint, cached briefly.
        Verdict is UNKNOWN whenever we can't tell — including when no API
        key is configured — and UNKNOWN never blocks a trade."""
        if not self.enabled:
            return {"verdict": UNKNOWN, "sellers": 0, "buyers": 0, "examined": 0}
        cached = self._cache.get(mint)
        if cached and time.time() - cached[1] < config.HONEYPOT_CACHE_TTL_SECONDS:
            return cached[0]
        transactions = await self.swap_history(mint)
        result = (analyze_swaps(transactions, mint) if transactions is not None
                  else {"verdict": UNKNOWN, "sellers": 0, "buyers": 0,
                        "examined": 0})
        self._cache[mint] = (result, time.time())
        return result
