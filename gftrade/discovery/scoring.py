"""
Composite 0-100 quality score for a screened pair. The hard filters
(filters.py) decide *eligible*; this decides *how good*, and the alert /
autobuy thresholds in settings cut on it.

Five weighted components, each normalized to 0-1:

  momentum   (25) — short-term trend that's positive but not vertical.
                    Vertical (+150% in an hour) is a pump profile, and
                    chasing it is exit liquidity — it's penalized.
  volume     (20) — right-now surge vs the hour's pace, plus 24h turnover
                    relative to pool size (is anyone actually trading it).
  organic    (20) — buy counts and a buy/sell ratio in the healthy band:
                    demand-tilted (>1) but not wash-shaped (<~3).
  liquidity  (15) — absolute depth plus liq/mcap inside the sane band.
  safety     (20) — renounced mint, no freeze authority, dispersed holders.
                    Unknown checks get half credit (or zero when
                    security_strict is on — strict mode shouldn't let a
                    dead RPC inflate scores).

Like everything in discovery/, a tunable heuristic — not a validated edge.
"""


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _momentum(pair: dict) -> float:
    change = pair.get("priceChange") or {}
    m5 = change.get("m5", 0) or 0
    h1 = change.get("h1", 0) or 0
    if m5 <= 0 or h1 <= 0:
        return 0.0
    base = 0.4 * _clamp(m5 / 5) + 0.6 * _clamp(h1 / 30)
    overheat = _clamp((h1 - 100) / 150)  # +100% h1 starts bleeding score, +250% zeroes it
    return _clamp(base * (1 - overheat))


def _volume(pair: dict) -> float:
    volume = pair.get("volume") or {}
    vol_5m, vol_1h, vol_24h = (volume.get("m5", 0) or 0), (volume.get("h1", 0) or 0), (volume.get("h24", 0) or 0)
    liquidity = (pair.get("liquidity") or {}).get("usd", 0) or 0
    surge = 0.0
    if vol_1h > 0:
        surge = _clamp((vol_5m / (vol_1h / 12)) / 4)  # 4x the hourly pace = full marks
    turnover = _clamp((vol_24h / liquidity) / 5) if liquidity > 0 else 0.0
    return 0.6 * surge + 0.4 * turnover


def _organic(pair: dict) -> float:
    txns = pair.get("txns") or {}
    buys_5m = (txns.get("m5") or {}).get("buys", 0)
    sells_5m = (txns.get("m5") or {}).get("sells", 0)
    buys_h1 = (txns.get("h1") or {}).get("buys", 0)
    activity = 0.5 * _clamp(buys_5m / 25) + 0.5 * _clamp(buys_h1 / 150)
    ratio = buys_5m / max(sells_5m, 1)
    if 1.0 <= ratio <= 3.0:
        balance = 1.0                      # demand-tilted, still two-sided
    elif ratio < 1.0:
        balance = _clamp(ratio)            # net selling scales toward 0
    else:
        balance = _clamp(1 - (ratio - 3.0) / 3.0)  # wash-shaped scales toward 0
    return 0.6 * activity + 0.4 * balance


def _liquidity(pair: dict) -> float:
    liquidity = (pair.get("liquidity") or {}).get("usd", 0) or 0
    market_cap = pair.get("marketCap") or pair.get("fdv") or 0
    depth = _clamp((liquidity - 10_000) / 90_000)  # $10k..$100k -> 0..1
    band = 0.0
    if market_cap > 0:
        ratio = liquidity / market_cap
        # sweet spot around 8-35%; filters already cut the extremes
        band = 1.0 if 0.08 <= ratio <= 0.35 else 0.5
    return 0.7 * depth + 0.3 * band


def _safety(report, strict: bool) -> float:
    from .. import config  # local import: scoring stays otherwise config-free

    if report is None:
        return 0.0 if strict else 0.5
    unknown_credit = 0.0 if strict else 0.5

    def credit(value) -> float:
        if value is None:
            return unknown_credit
        return 1.0 if value else 0.0

    top10_ok = None
    if report.top10_pct is not None:
        # full credit under 15%, fading to zero by 40%
        top10_ok = _clamp((40 - report.top10_pct) / 25)

    if not config.LP_CHECK_ENABLED:
        return (
            0.4 * credit(report.mint_renounced)
            + 0.3 * credit(report.freeze_none)
            + 0.3 * (top10_ok if top10_ok is not None else unknown_credit)
        )
    lp_ok = None
    if getattr(report, "lp_locked_pct", None) is not None:
        lp_ok = _clamp(report.lp_locked_pct / 100)
    return (
        0.3 * credit(report.mint_renounced)
        + 0.25 * credit(report.freeze_none)
        + 0.25 * (top10_ok if top10_ok is not None else unknown_credit)
        + 0.2 * (lp_ok if lp_ok is not None else unknown_credit)
    )


WEIGHTS = {"momentum": 25, "volume": 20, "organic": 20, "liquidity": 15, "safety": 20}
MARKET_WEIGHTS = {"momentum": 25, "volume": 20, "organic": 20, "liquidity": 15}


def market_score_pair(pair: dict) -> int:
    """Pure market quality on 0-100 — the same four market components as
    the combined score, renormalized WITHOUT safety. Risk is reported
    separately (a tier), so a hot chart can't launder a risky coin into
    the same number as a safe one."""
    components = {
        "momentum": _momentum(pair),
        "volume": _volume(pair),
        "organic": _organic(pair),
        "liquidity": _liquidity(pair),
    }
    points = sum(MARKET_WEIGHTS[name] * value for name, value in components.items())
    total = sum(MARKET_WEIGHTS.values())
    return max(0, min(100, int(round(points * 100 / total))))


def score_pair(pair: dict, safety_report=None, strict: bool = True) -> tuple:
    """Returns (score: int 0-100, breakdown: {component: points})."""
    components = {
        "momentum": _momentum(pair),
        "volume": _volume(pair),
        "organic": _organic(pair),
        "liquidity": _liquidity(pair),
        "safety": _safety(safety_report, strict),
    }
    breakdown = {name: round(WEIGHTS[name] * value, 1) for name, value in components.items()}
    score = int(round(sum(breakdown.values())))
    return max(0, min(100, score)), breakdown
