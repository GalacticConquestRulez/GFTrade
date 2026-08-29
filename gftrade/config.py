"""
Static configuration.

Secrets and deployment-specific values come from environment variables
(a local `.env` file is loaded if present — see `.env.example`). Strategy
thresholds live here as plain constants so they're easy to read and tune.

Settings a user is expected to tweak day-to-day (slippage, TP/SL, buy
presets, autobuy, ...) are NOT here — they live in DEFAULT_SETTINGS,
persist to the state file, and are editable live from Telegram (/settings).

DRY_RUN is deliberately env-only: switching to live trading should require
touching the deployment, not tapping a button in a chat.
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _parse_ids(raw: str) -> list:
    """Comma-separated Telegram ids -> [int]; junk entries are dropped."""
    ids = []
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            pass
    return ids


# --- Telegram ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# Authorized Telegram user ids, comma-separated (TELEGRAM_USER_IDS). Every
# listed user can fully drive the bot — trade, change settings, panic-sell —
# and receives every scanner alert; anyone else is silently ignored, because
# this bot controls a wallet. The FIRST id is the primary owner: private-key
# export is restricted to them. TELEGRAM_CHAT_ID is honored as a single-id
# fallback so older configs keep working.
AUTHORIZED_IDS = _parse_ids(
    os.getenv("TELEGRAM_USER_IDS") or os.getenv("TELEGRAM_CHAT_ID") or ""
)
OWNER_ID = AUTHORIZED_IDS[0] if AUTHORIZED_IDS else 0

# --- Chain / execution ---
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
JUPITER_API_KEY = os.getenv("JUPITER_API_KEY", "")
# With a (free) portal.jup.ag key we use the keyed endpoint; without one we
# fall back to the keyless lite endpoint, which is fine for light usage.
JUPITER_API_BASE = os.getenv(
    "JUPITER_API_BASE",
    "https://api.jup.ag" if JUPITER_API_KEY else "https://lite-api.jup.ag",
)

# True  -> screen + score + simulate fills against live prices; nothing is
#          ever sent to the blockchain and no wallet is required.
# False -> real swaps through Jupiter with the wallet in WALLET_KEYFILE.
DRY_RUN = _env_bool("DRY_RUN", True)

WALLET_KEYFILE = os.getenv("WALLET_KEYFILE", "wallet.json")
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# --- Scanner cadence ---
SCAN_INTERVAL_SECONDS = _env_int("SCAN_INTERVAL_SECONDS", 90)

# --- Hard screening thresholds (discovery/filters.py) ---
# None of these are research-backed magic numbers — they're sane starting
# points for an adversarial, shifting target. Tune against your own logs.
QUOTE_TOKENS = ["SOL", "USDC"]   # only consider pairs quoted against these
MIN_PAIR_AGE_MINUTES = 20        # brand-new pairs are peak rug/honeypot territory; let them breathe first
MAX_PAIR_AGE_HOURS = 12          # "find them early" — stop caring once a pair is half a day old
MIN_LIQUIDITY_USD = 10_000       # below this, slippage/rug risk is extreme
MIN_LIQ_TO_MCAP_RATIO = 0.05     # cap inflated vs what actually backs the price -> easy to dump on
MAX_LIQ_TO_MCAP_RATIO = 0.60     # anomalously high liq vs FDV shows up in wash-traded/seeded pools
MIN_VOLUME_H1_USD = 5_000        # dead pools don't fill exits
MIN_BUYS_5M = 5                  # organic-activity floor, 5-minute window
MIN_BUYS_H1 = 25                 # and over the last hour, so one burst can't qualify a pair
MAX_BUY_SELL_IMBALANCE = 4.0     # buys:sells above this looks like wash trading, not demand
EXCLUDE_BOOSTED = True           # drop anything paying DexScreener for placement

# --- On-chain safety screen (discovery/safety.py) ---
# Top-10 holder share EXCLUDING the single largest account (which is almost
# always the liquidity pool itself on a new pair).
MAX_TOP10_HOLDER_PCT = 30.0
SAFETY_CACHE_TTL_SECONDS = 600

# --- LP lock screening (RugCheck) ---
# "Liquidity locked/burned" can't be read generically from raw RPC without
# per-DEX pool layout parsing, so this uses RugCheck's public API. If the
# API is unreachable the LP status is UNKNOWN — which strict mode rejects
# (no lock proof, no trade) and lenient mode allows. Set
# LP_CHECK_ENABLED=false to drop the requirement entirely.
LP_CHECK_ENABLED = _env_bool("LP_CHECK_ENABLED", True)
RUGCHECK_API_BASE = os.getenv("RUGCHECK_API_BASE", "https://api.rugcheck.xyz/v1")
MIN_LP_LOCKED_PCT = 80.0         # at least this % of LP must be locked/burned

# --- Discovery bookkeeping ---
CANDIDATE_POOL_MAX = 400         # mints tracked for re-checks between scans
ALERT_COOLDOWN_HOURS = 24        # don't re-alert the same mint within this window

# --- Execution details ---
PRIORITY_FEE = "auto"            # Jupiter prioritizationFeeLamports; "auto" or an int of lamports
BALANCE_BUFFER_SOL = 0.01        # always leave this much SOL for fees/rent
SIM_START_BALANCE_SOL = 1.0      # dry-run paper wallet starting balance
SIM_FEE_PCT = 0.005              # per-side cost (DEX fee + assumed impact) applied to simulated fills

# --- Runtime-tunable settings (persisted in the state file, edited via /settings) ---
DEFAULT_SETTINGS = {
    "scanner_on": True,          # background discovery loop
    "autobuy": False,            # act on signals automatically (OFF until you trust the filters)
    "autobuy_sol": 0.05,         # size per auto entry
    "buy_presets": [0.1, 0.5, 1.0],  # SOL amounts on quick-buy buttons
    "slippage_bps": 200,         # 2% — new pairs move; tighter values fail to fill
    "take_profit_pct": 35.0,
    "stop_loss_pct": 30.0,
    "trailing_stop_pct": 0.0,    # 0 = off; >0 arms a stop that follows the peak price
    # Staged exits: at take-profit, sell only this % (100 = sell everything).
    # The remainder ("the runner") is then protected by a trailing stop that
    # never triggers below the entry price (a hard one-tick crash can still
    # fill lower — monitored stops aren't resting orders).
    "tp_sell_pct": 50.0,
    "runner_trailing_pct": 20.0,
    "min_alert_score": 70,       # 0-100 composite score needed to alert
    "min_autobuy_score": 82,     # stricter bar before money moves on its own
    "max_positions": 3,
    "security_strict": True,     # True: unknown/failed on-chain safety checks reject a token
    # Market-screen thresholds, editable from /settings so tuning them
    # doesn't need a server edit + restart. These override the constants
    # above at runtime; the constants remain the documented defaults.
    "min_liquidity_usd": float(MIN_LIQUIDITY_USD),
    "min_volume_h1_usd": float(MIN_VOLUME_H1_USD),
    "min_buys_h1": MIN_BUYS_H1,
    "max_pair_age_hours": float(MAX_PAIR_AGE_HOURS),
}
