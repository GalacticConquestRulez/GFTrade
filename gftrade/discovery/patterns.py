"""
DexScreener gives point-in-time snapshots (volume/txns/price-change over
rolling windows), not OHLCV candles — so patterns here score momentum and
accumulation shape from those fields rather than classic chart patterns.

Contract: each function takes a pair dict and returns
(triggered: bool, confidence: float 0-1, name: str), scoring likelihood of
continuation, not predicted magnitude. A triggered pattern is required
before the scanner alerts — the composite score (scoring.py) measures
quality, a pattern says "something is happening right now".

These are starting points, not a validated edge. Add your own with the
same signature and append to ALL_PATTERNS.
"""


def accumulation_momentum(pair: dict):
    """Steady, broad-based buying: rising 1h and 5m price change together,
    with a buy-heavy but not absurd txn ratio (rules out one whale pump)."""
    price_change = pair.get("priceChange") or {}
    txns_5m = (pair.get("txns") or {}).get("m5") or {}
    m5_change, h1_change = price_change.get("m5", 0) or 0, price_change.get("h1", 0) or 0
    buys, sells = txns_5m.get("buys", 0), txns_5m.get("sells", 0)

    triggered = m5_change > 0 and h1_change > 0 and buys > sells and buys >= 8
    if not triggered:
        return False, 0.0, "accumulation_momentum"

    buy_ratio = min(buys / max(sells, 1), 5.0) / 5.0
    trend_strength = min(h1_change / 20, 1.0)  # cap at +20% h1 for scoring purposes
    confidence = 0.45 + 0.3 * buy_ratio + 0.25 * trend_strength
    return True, round(min(confidence, 1.0), 3), "accumulation_momentum"


def volume_surge(pair: dict):
    """5-minute volume disproportionate to the last hour's average pace —
    something is happening right now, not just steady drift."""
    volume = pair.get("volume") or {}
    vol_5m, vol_1h = volume.get("m5", 0) or 0, volume.get("h1", 0) or 0
    if vol_1h <= 0:
        return False, 0.0, "volume_surge"

    expected_5m_share = vol_1h / 12  # 12 five-minute windows per hour
    surge_ratio = vol_5m / expected_5m_share
    if surge_ratio <= 2.5:
        return False, 0.0, "volume_surge"

    confidence = 0.5 + 0.4 * min((surge_ratio - 2.5) / 5, 1.0)
    return True, round(min(confidence, 1.0), 3), "volume_surge"


def healthy_liquidity_growth(pair: dict):
    """Moderate sustained gain (not a vertical spike) with liquidity
    comfortably above the floor. We can't see historical liquidity in one
    snapshot, so price stability is the proxy: thinning liquidity under a
    rising price — the classic reversal setup — tends to show up as the
    spiky profile this pattern refuses to match."""
    price_change = pair.get("priceChange") or {}
    liquidity_usd = (pair.get("liquidity") or {}).get("usd", 0) or 0
    h6_change = price_change.get("h6", 0) or 0

    triggered = 3 <= h6_change <= 40 and liquidity_usd > 15_000
    if not triggered:
        return False, 0.0, "healthy_liquidity_growth"

    confidence = 0.5 + 0.3 * min(liquidity_usd / 50_000, 1.0)
    return True, round(min(confidence, 1.0), 3), "healthy_liquidity_growth"


ALL_PATTERNS = [accumulation_momentum, volume_surge, healthy_liquidity_growth]


def scan(pair: dict) -> list:
    """All triggered patterns for a pair, strongest first.
    Each entry: {"pattern": name, "confidence": float}."""
    hits = []
    for fn in ALL_PATTERNS:
        triggered, confidence, name = fn(pair)
        if triggered:
            hits.append({"pattern": name, "confidence": confidence})
    return sorted(hits, key=lambda h: -h["confidence"])
