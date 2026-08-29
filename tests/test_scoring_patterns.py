from gftrade.discovery import patterns, scoring
from gftrade.discovery.safety import SafetyReport

from conftest import GOOD_SAFETY, make_pair, make_strong_pair


def test_score_bounds_and_breakdown():
    score, breakdown = scoring.score_pair(make_pair(), GOOD_SAFETY, strict=True)
    assert 0 <= score <= 100
    assert set(breakdown) == set(scoring.WEIGHTS)
    for name, points in breakdown.items():
        assert 0 <= points <= scoring.WEIGHTS[name]


def test_strong_pair_beats_weak_pair():
    strong, _ = scoring.score_pair(make_strong_pair(), GOOD_SAFETY, strict=True)
    weak, _ = scoring.score_pair(
        make_pair(chg_m5=-1, chg_h1=-5, buys_5m=5, vol_m5=500), GOOD_SAFETY, strict=True
    )
    assert strong > weak
    assert strong >= 82  # must clear the default autobuy bar for scanner tests


def test_unsafe_token_scores_lower():
    bad_safety = SafetyReport(mint="x", mint_renounced=False, freeze_none=False,
                              top10_pct=80.0)
    safe_score, _ = scoring.score_pair(make_pair(), GOOD_SAFETY, strict=True)
    unsafe_score, _ = scoring.score_pair(make_pair(), bad_safety, strict=True)
    assert unsafe_score < safe_score


def test_unknown_safety_strict_vs_lenient():
    unknown = SafetyReport(mint="x")
    strict_score, _ = scoring.score_pair(make_pair(), unknown, strict=True)
    lenient_score, _ = scoring.score_pair(make_pair(), unknown, strict=False)
    assert lenient_score > strict_score


def test_vertical_pump_is_penalized():
    steady, _ = scoring.score_pair(make_pair(chg_h1=25), GOOD_SAFETY, strict=True)
    vertical, _ = scoring.score_pair(make_pair(chg_h1=300), GOOD_SAFETY, strict=True)
    assert vertical < steady


def test_patterns_trigger_and_confidence_bounds():
    hits = patterns.scan(make_strong_pair())
    assert hits, "strong pair should trigger at least one pattern"
    for hit in hits:
        assert 0.0 <= hit["confidence"] <= 1.0
    # sorted strongest first
    confidences = [h["confidence"] for h in hits]
    assert confidences == sorted(confidences, reverse=True)


def test_patterns_quiet_pair_no_trigger():
    quiet = make_pair(chg_m5=-2, chg_h1=-10, chg_h6=-5, buys_5m=3, sells_5m=9,
                      vol_m5=100, vol_h1=30_000)
    assert patterns.scan(quiet) == []
