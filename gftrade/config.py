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
# Second RPC used ONLY when the primary errors out: the primary can then be
# a faster-but-newer endpoint without that being a single point of failure.


def _helius_gatekeeper(url: str) -> str:
    """Gatekeeper (edge-gateway) equivalent of a Helius mainnet URL, or ""
    if the URL isn't one. Same API key, same responses, tens to hundreds of
    ms faster per call because it skips the old CDN hop."""
    if "mainnet.helius-rpc.com" not in (url or ""):
        return ""
    return url.replace("mainnet.helius-rpc.com", "beta.helius-rpc.com")


# Auto-pairing: given a plain Helius mainnet RPC and no explicit fallback,
# use Gatekeeper as the primary and the configured mainnet URL as the
# standby. This is derived from the key already in the environment, so the
# faster path needs no extra configuration and no second copy of the key —
# and any hiccup on the beta endpoint falls back to the stable one
# automatically. Set HELIUS_GATEKEEPER=false to stay on plain mainnet, or
# set SOLANA_RPC_FALLBACK_URL explicitly to define both ends yourself.
_EXPLICIT_FALLBACK = os.getenv("SOLANA_RPC_FALLBACK_URL", "")
if _EXPLICIT_FALLBACK:
    SOLANA_RPC_FALLBACK_URL = _EXPLICIT_FALLBACK
elif _env_bool("HELIUS_GATEKEEPER", True) and _helius_gatekeeper(SOLANA_RPC_URL):
    SOLANA_RPC_FALLBACK_URL = SOLANA_RPC_URL
    SOLANA_RPC_URL = _helius_gatekeeper(SOLANA_RPC_URL)
else:
    SOLANA_RPC_FALLBACK_URL = ""
# After this many consecutive primary failures, calls go straight to the
# fallback for a cooldown instead of paying the primary's timeout each time.
RPC_FAILOVER_AFTER = 3
RPC_FAILOVER_COOLDOWN_SECONDS = 300
# Client-side ceiling on RPC requests per second, enforced where requests
# leave (solana_rpc.RateLimiter). Providers cap this per plan — Helius
# allows 10/s on the free tier and 50/s on the $49 Developer plan — and a
# discovery pass is bursty: verifying one fresh coin costs ~6 calls (mint
# info, holders, and four more for the on-chain LP read), so 8 coins in a
# pass is ~48 calls fired back to back. Default sits just under the free
# tier; raise it to ~45 on Developer for faster sweeps. 0 disables.
RPC_MAX_RPS = float(os.getenv("RPC_MAX_RPS", "45") or 45)
# JSON-RPC array batching: how many calls ride in one HTTP POST.
RPC_BATCH_SIZE = _env_int("RPC_BATCH_SIZE", 100)
# How a batch is charged against RPC_MAX_RPS. Providers differ on whether
# a 100-call array counts as one request or a hundred, and getting it
# wrong means either 429s or needless slowness — so it is a setting, not
# an assumption baked into code. "1" = one slot per batch (the common
# behavior); "size" = one slot per call inside it. Measure, then set.
RPC_BATCH_COUNTS_AS = (os.getenv("RPC_BATCH_COUNTS_AS", "1") or "1").strip().lower()
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
# Discovery (feeds + screening + scoring) runs every SCAN_INTERVAL_SECONDS.
# Open-position exits (TP/SL/trailing/runner) are checked far more often —
# with 90s checks a -10% stop on a fast coin routinely fills at -15%,
# because price falls straight through the level between looks. Each exit
# check is ONE batched DexScreener pair call regardless of position count:
# at 5s that's 12/min, plus ~6/min from discovery — far under the 300/min
# pair-endpoint limit. Raise this if you ever see 429s in the logs.
SCAN_INTERVAL_SECONDS = _env_int("SCAN_INTERVAL_SECONDS", 90)
EXIT_CHECK_INTERVAL_SECONDS = _env_int("EXIT_CHECK_INTERVAL_SECONDS", 5)

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
# Backup LP-lock source (keyless, ~30 req/min, one mint per request):
# consulted only when RugCheck can't answer, so one service's outage or
# index gap doesn't leave coins stuck at ❓ unverified.
GOPLUS_API_BASE = os.getenv("GOPLUS_API_BASE", "https://api.gopluslabs.io/api/v1")
MIN_LP_LOCKED_PCT = 80.0         # at least this % of LP must be locked/burned


def _helius_key_from(url: str) -> str:
    """The api-key out of a Helius URL, so a Helius RPC entry alone is
    enough to enable the enhanced endpoints — no second variable to set."""
    if "helius" not in (url or ""):
        return ""
    from urllib.parse import parse_qs, urlparse
    return (parse_qs(urlparse(url).query).get("api-key") or [""])[0].strip()


# --- Honeypot verification (clients/helius.py) ---
# Helius's Enhanced Transactions API returns human-readable swap history
# for a mint, which answers the one question aggregate market data can't:
# has anyone actually SOLD this token successfully? Buys with zero sells
# is the honeypot signature — you get in, you can't get out.
#
# It costs 100 credits per call (vs 1 for a normal RPC call), so this is
# NOT a screening step: it runs once per mint, cached, immediately before
# money moves. A handful of buys a day is a rounding error on the budget;
# calling it for every candidate in every sweep would burn a free month's
# credits in a day.
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "") or _helius_key_from(SOLANA_RPC_URL) \
    or _helius_key_from(SOLANA_RPC_FALLBACK_URL)
HELIUS_API_BASE = os.getenv("HELIUS_API_BASE", "https://mainnet.helius-rpc.com")
HONEYPOT_TX_LOOKBACK = 100       # recent swaps examined per check
HONEYPOT_MIN_SELLERS = 2         # distinct wallets that must have sold successfully
HONEYPOT_MIN_BUYS_FOR_VERDICT = 12   # below this, too little history to judge
HONEYPOT_CACHE_TTL_SECONDS = 300

# --- Discovery bookkeeping ---
CANDIDATE_POOL_MAX = 400         # mints tracked for re-checks between scans
ALERT_COOLDOWN_HOURS = 24        # don't re-alert the same mint within this window

# --- Factor logging (factors.py / analysis.py) ---
FACTOR_DB = os.getenv("FACTOR_DB", "factor_log.db")
# Safety verdicts persist here so a restart does not re-vet the whole
# candidate pool from scratch (slow, and wasted API credits).
SAFETY_CACHE_DB = os.getenv("SAFETY_CACHE_DB", "safety_cache.db")
FACTOR_DEDUPE_MINUTES = 30       # at most one snapshot per mint per this window

# --- Volatility-scaled exits (opt-in via the vol_scaled_exits setting) ---
# TP/SL percentages are multiplied by (token volatility / reference),
# clamped to the band below, so a wild coin gets wider exits and a calm
# one tighter — instead of one flat percentage for both.
VOL_REFERENCE_PCT = 3.0          # point-to-point stdev (%) that maps to factor 1.0
VOL_FACTOR_MIN = 0.5
VOL_FACTOR_MAX = 2.0

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
    # Extra age gate for AUTOBUY only, in minutes. 0 = autobuy follows the
    # global min_pair_age_minutes like everything else. Setting it higher
    # lets alerts fire young (manual flip territory) while the bot's own
    # buying waits for a coin to survive longer first.
    "autobuy_min_age_minutes": 0.0,
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
    # Trend-stage gate: block alerts/autobuy when price is already more
    # than this % above its own lowest observed price in the last hour —
    # "up 80% off the low" is late in a move, and late entries are how a
    # pump gets ridden through the stop. 0 disables. Manual buys are never
    # blocked; the token card shows the extension instead.
    "max_entry_extension_pct": 60.0,
    # Scale TP/SL by each token's own recent volatility (see VOL_* above).
    # Off by default so dialed-in flat percentages keep meaning what they say.
    "vol_scaled_exits": False,
    # /scan view filter: True lists only fully-✅ coins (LP lock proven,
    # renounced, the works) and reports how many were hidden; False shows
    # the whole badged field. Alerts/autobuy gating is unaffected either way.
    "scan_safe_only": False,
    "min_alert_score": 70,       # 0-100 composite score needed to alert
    "min_autobuy_score": 82,     # stricter bar before money moves on its own
    "max_positions": 3,
    "security_strict": True,     # True: unknown/failed on-chain safety checks reject a token
    # Also alert on screened coins whose safety is merely UNVERIFIED (❓) —
    # for manual small-size flips. Coins with a KNOWN-BAD check (freeze
    # authority on, mint authority live, verified-unlocked LP, whale-heavy
    # holders) never alert regardless: honeypots specifically farm
    # flippers, and one of them erases ten winning flips. Autobuy ignores
    # this flag entirely — money only moves itself on fully-✅ coins.
    "alert_unverified": False,
    # Verify on-chain that real wallets have successfully SOLD a token
    # before buying it (see HONEYPOT_* above). Blocks the buy — auto or
    # manual — only on positive evidence: plenty of buys, no sells. An
    # unreachable API, a thin history, or an unparseable answer is
    # UNKNOWN and never blocks. Costs 100 Helius credits per new mint.
    "honeypot_check": True,
    # Market-screen thresholds, editable from /settings so tuning them
    # doesn't need a server edit + restart. These override the constants
    # above at runtime; the constants remain the documented defaults.
    "min_liquidity_usd": float(MIN_LIQUIDITY_USD),
    "min_volume_h1_usd": float(MIN_VOLUME_H1_USD),
    "min_buys_h1": MIN_BUYS_H1,
    "max_pair_age_hours": float(MAX_PAIR_AGE_HOURS),
    # Minimum pair age before a coin can screen (and thus alert/autobuy).
    # 0 disables the delay entirely. The first minutes of a pair's life are
    # peak rug/honeypot territory — lowering this trades safety margin for
    # earlier entries; autobuy still requires every safety check to pass.
    "min_pair_age_minutes": float(MIN_PAIR_AGE_MINUTES),
}
