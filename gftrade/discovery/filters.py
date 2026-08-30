"""
Hard screening: takes a raw DexScreener pair object and decides whether
it's worth considering at all, BEFORE any scoring. This is the "ignore
promoted, sanity-check liquidity vs market cap, demand organic activity"
layer.

None of these thresholds are magic numbers backed by research — they're
reasonable starting points (config.py) to tune as you watch real results.
"Manipulated vs organic" is a fuzzy, adversarial target that shifts as
manipulators adapt; expect to revisit these.
"""
import time

from .. import config


def pair_age_hours(pair: dict, now_ms: float = None) -> float:
    created_at_ms = pair.get("pairCreatedAt")
    if not created_at_ms:
        return -1.0
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    return (now_ms - created_at_ms) / (1000 * 60 * 60)


def screen_pair(pair: dict, boosted_addresses: set = None, now_ms: float = None,
                overrides: dict = None) -> tuple:
    """Returns (passed: bool, reasons: list[str]). `reasons` explains every
    rejection (useful for logging/tuning) and is empty when passed.

    `overrides` (usually the runtime settings dict) can replace the
    tunable thresholds; anything missing falls back to config."""
    reasons = []
    boosted_addresses = boosted_addresses or set()
    overrides = overrides or {}
    min_liquidity = overrides.get("min_liquidity_usd", config.MIN_LIQUIDITY_USD)
    min_volume_h1 = overrides.get("min_volume_h1_usd", config.MIN_VOLUME_H1_USD)
    min_buys_h1 = overrides.get("min_buys_h1", config.MIN_BUYS_H1)
    max_age_hours = overrides.get("max_pair_age_hours", config.MAX_PAIR_AGE_HOURS)
    min_age_minutes = overrides.get("min_pair_age_minutes", config.MIN_PAIR_AGE_MINUTES)

    chain_id = pair.get("chainId")
    base_token = pair.get("baseToken") or {}
    token_address = (base_token.get("address") or "").lower()

    # 1. Exclude anything paying for promotion — both the boost feeds and
    #    the per-pair boosts field (they don't always agree).
    if config.EXCLUDE_BOOSTED:
        actively_boosted = ((pair.get("boosts") or {}).get("active") or 0) > 0
        if actively_boosted or (chain_id, token_address) in boosted_addresses:
            reasons.append("boosted/promoted (paid placement)")

    # 2. Quote token must be one we're willing to trade against
    quote_symbol = (pair.get("quoteToken") or {}).get("symbol", "")
    if quote_symbol not in config.QUOTE_TOKENS:
        reasons.append(f"quote token {quote_symbol or '?'} not in {config.QUOTE_TOKENS}")

    # 3. Age window: too new = peak rug/honeypot territory, too old = not
    #    the "catch it early" game this bot plays.
    age_h = pair_age_hours(pair, now_ms)
    if age_h < 0:
        reasons.append("no pair creation timestamp")
    elif age_h * 60 < min_age_minutes:
        reasons.append(f"pair only {age_h * 60:.0f}m old, min {min_age_minutes:g}m")
    elif age_h > max_age_hours:
        reasons.append(f"pair age {age_h:.1f}h exceeds max {max_age_hours:g}h")

    # 4. Absolute liquidity floor
    liquidity_usd = (pair.get("liquidity") or {}).get("usd") or 0
    if liquidity_usd < min_liquidity:
        reasons.append(f"liquidity ${liquidity_usd:,.0f} below floor ${min_liquidity:,.0f}")

    # 5. Liquidity-to-market-cap ratio — the core "is this manipulated"
    #    heuristic. Very low: the cap isn't backed by real depth, one sell
    #    crashes it (or it's low-float manipulation). Very high vs FDV:
    #    often a wash-traded or freshly seeded pool.
    market_cap = pair.get("marketCap") or pair.get("fdv") or 0
    if market_cap > 0:
        ratio = liquidity_usd / market_cap
        if ratio < config.MIN_LIQ_TO_MCAP_RATIO:
            reasons.append(f"liq/mcap {ratio:.3f} below min {config.MIN_LIQ_TO_MCAP_RATIO}")
        elif ratio > config.MAX_LIQ_TO_MCAP_RATIO:
            reasons.append(f"liq/mcap {ratio:.3f} above max {config.MAX_LIQ_TO_MCAP_RATIO}")
    else:
        reasons.append("no market cap data")

    # 6. Volume floor — dead pools don't fill exits
    volume_h1 = (pair.get("volume") or {}).get("h1") or 0
    if volume_h1 < min_volume_h1:
        reasons.append(f"1h volume ${volume_h1:,.0f} below floor ${min_volume_h1:,.0f}")

    # 7. Organic-activity checks on transaction counts. Successful sells are
    #    the only market-data evidence that selling WORKS — many buys with
    #    zero sells is the honeypot signature, not enthusiasm.
    txns = pair.get("txns") or {}
    buys_5m = (txns.get("m5") or {}).get("buys", 0)
    sells_5m = (txns.get("m5") or {}).get("sells", 0)
    buys_h1 = (txns.get("h1") or {}).get("buys", 0)
    sells_h1 = (txns.get("h1") or {}).get("sells", 0)
    if buys_5m < config.MIN_BUYS_5M:
        reasons.append(f"only {buys_5m} buys in 5m, floor {config.MIN_BUYS_5M}")
    if buys_h1 < min_buys_h1:
        reasons.append(f"only {buys_h1} buys in 1h, floor {min_buys_h1:g}")
    if sells_5m > 0 and buys_5m / sells_5m > config.MAX_BUY_SELL_IMBALANCE:
        reasons.append(
            f"5m buy/sell ratio {buys_5m / sells_5m:.1f} looks like wash trading"
        )
    if buys_h1 >= 10 and sells_h1 == 0:
        reasons.append(
            f"{buys_h1} buys but ZERO sells in 1h — honeypot signature "
            "(no evidence anyone can sell)"
        )
    elif sells_h1 > 0 and buys_h1 / sells_h1 > config.MAX_BUY_SELL_IMBALANCE:
        reasons.append(
            f"1h buy/sell ratio {buys_h1 / sells_h1:.1f} looks like wash "
            "trading or throttled sells"
        )

    return (len(reasons) == 0, reasons)


# Stable buckets for the human-readable reasons above, so the scanner can
# report WHICH screen is rejecting the most coins (see /filters). The
# mapping lives next to the strings it classifies; a test asserts every
# reason screen_pair can emit lands in a real bucket, so the two can't
# drift apart silently.
# Each needle must be unique to ONE reason string above. Beware of short
# words: "min" alone also matches "liq/mcap 0.000 below min 0.05".
REASON_CODES = [
    ("boosted", "boosted/paid placement"),
    ("quote token", "quote token not SOL/USDC"),
    ("no pair creation", "no creation timestamp"),
    ("liq/mcap", "liquidity vs market-cap band"),
    ("pair only", "too young"),
    ("exceeds max", "too old"),
    ("1h volume", "volume below floor"),
    ("liquidity $", "liquidity below floor"),
    ("no market cap", "no market-cap data"),
    ("buys in 5m", "too few buys (5m)"),
    ("buys in 1h", "too few buys (1h)"),
    ("wash trading", "buy/sell ratio (wash-trading cap)"),
    ("ZERO sells", "honeypot signature (no sells)"),
]


def classify_reason(reason: str) -> str:
    """Bucket one rejection reason into a stable label for the /filters
    histogram. Unrecognized text returns "other" — a test asserts that
    never happens for reasons screen_pair actually emits."""
    text = str(reason or "")
    for needle, label in REASON_CODES:
        if needle in text:
            return label
    return "other"
