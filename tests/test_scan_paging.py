"""Paged /scan view: best ranked first, arrows to move through pages."""
import time
from types import SimpleNamespace

from gftrade.tg.handlers import SCAN_PAGE_SIZE, scan_page_view

from conftest import make_pair


def make_verdicts(count, safety_ok=True, safety=None):
    verdicts = []
    for i in range(count):
        mint = chr(ord("C") + i) * 40 + "zzzz"
        pair = make_pair(mint=mint, symbol=f"TK{i}")
        verdicts.append({
            "pair": pair, "mint": mint, "score": 95 - i, "breakdown": {},
            "patterns": [{"pattern": "volume_surge", "confidence": 0.7}],
            "safety": safety, "safety_ok": safety_ok,
            "screened_ok": True, "reject_reasons": [],
        })
    return verdicts


def deps_with(verdicts):
    return SimpleNamespace(
        scanner=SimpleNamespace(last_scan={"verdicts": verdicts, "at": time.time()}),
        store=SimpleNamespace(settings={}),
    )


def buttons_of(markup):
    return [button for row in markup.inline_keyboard for button in row]


def test_first_page_has_next_but_no_prev():
    deps = deps_with(make_verdicts(12))
    text, markup = scan_page_view(deps, 0)
    assert "page 1/3" in text
    assert "#1 TK0" in text and "95/100" in text
    labels = [b.text for b in buttons_of(markup)]
    assert any("Next ▶️" == l for l in labels)
    assert not any("◀️ Prev" == l for l in labels)
    # one view button per listed token
    view_buttons = [b for b in buttons_of(markup)
                    if (b.callback_data or "").startswith("r:")]
    assert len(view_buttons) == SCAN_PAGE_SIZE


def test_middle_page_has_both_arrows_and_absolute_ranks():
    deps = deps_with(make_verdicts(12))
    text, markup = scan_page_view(deps, 1)
    assert "page 2/3" in text
    assert "#6 TK5" in text
    callbacks = [b.callback_data for b in buttons_of(markup)]
    assert "scp:0" in callbacks and "scp:2" in callbacks


def test_last_page_has_no_next_and_page_is_clamped():
    deps = deps_with(make_verdicts(12))
    text, markup = scan_page_view(deps, 99)  # clamped to the last page
    assert "page 3/3" in text
    assert "#11 TK10" in text
    labels = [b.text for b in buttons_of(markup)]
    assert not any("Next ▶️" == l for l in labels)
    assert any("◀️ Prev" == l for l in labels)


def test_ranked_best_first_across_pages():
    deps = deps_with(make_verdicts(7))
    page0, _ = scan_page_view(deps, 0)
    page1, _ = scan_page_view(deps, 1)
    assert "95/100" in page0 and "90/100" in page1  # scores descend across pages


def test_empty_scan_renders_explanation_with_rescan():
    deps = deps_with([])
    text, markup = scan_page_view(deps, 0)
    assert "Scan finished" in text
    callbacks = [b.callback_data for b in buttons_of(markup)]
    assert "scan" in callbacks  # re-scan stays available
    assert not any((c or "").startswith("scp:") for c in callbacks)


def test_empty_pool_points_at_feed_status():
    deps = SimpleNamespace(
        scanner=SimpleNamespace(
            last_scan={"verdicts": [], "at": time.time(), "evaluated": 0}),
        store=SimpleNamespace(settings={}),
    )
    text, _ = scan_page_view(deps, 0)
    assert "empty candidate pool" in text and "/start" in text
    # with candidates evaluated but none listable, the message differs
    deps.scanner.last_scan["evaluated"] = 40
    text, _ = scan_page_view(deps, 0)
    assert "empty candidate pool" not in text


def test_safety_flag_states():
    from gftrade.discovery.safety import SafetyReport
    from gftrade.tg import formatting as fmt

    assert fmt.safety_flag({"safety_ok": True}) == "✅"
    assert "mint active" in fmt.safety_flag(
        {"safety_ok": False, "safety": SafetyReport(mint="x", mint_renounced=False)})
    assert "freeze on" in fmt.safety_flag(
        {"safety_ok": False,
         "safety": SafetyReport(mint="x", mint_renounced=True, freeze_none=False)})
    assert "top10 55%" in fmt.safety_flag(
        {"safety_ok": False,
         "safety": SafetyReport(mint="x", mint_renounced=True, freeze_none=True,
                                top10_pct=55.0, lp_locked_pct=100.0)})
    assert "LP 5%" in fmt.safety_flag(
        {"safety_ok": False,
         "safety": SafetyReport(mint="x", mint_renounced=True, freeze_none=True,
                                top10_pct=10.0, lp_locked_pct=5.0)})
    assert "unverified" in fmt.safety_flag(
        {"safety_ok": False,
         "safety": SafetyReport(mint="x", mint_renounced=True, freeze_none=True,
                                top10_pct=10.0, lp_locked_pct=None)})


def test_header_counts_and_unverified_warning():
    from gftrade.discovery.safety import SafetyReport

    # mostly-unverified list -> warning shown, ❓ rows badged
    unverified = SafetyReport(mint="x", mint_renounced=True, freeze_none=True,
                              top10_pct=10.0, lp_locked_pct=None)
    deps = deps_with(make_verdicts(6, safety_ok=False, safety=unverified))
    text, _ = scan_page_view(deps, 0)
    assert "✅ 0 fully safe" in text
    assert "rate-limiting" in text
    assert "❓ unverified" in text

    # fully-verified list -> no warning
    deps = deps_with(make_verdicts(6, safety_ok=True))
    text, _ = scan_page_view(deps, 0)
    assert "✅ 6 fully safe" in text
    assert "rate-limiting" not in text


def test_badges_on_view_buttons():
    deps = deps_with(make_verdicts(3, safety_ok=False,
                                   safety=None))
    _, markup = scan_page_view(deps, 0)
    labels = [b.text for b in buttons_of(markup)
              if (b.callback_data or "").startswith("r:")]
    assert all(l.startswith("⚠️") for l in labels)
    deps = deps_with(make_verdicts(3, safety_ok=True))
    _, markup = scan_page_view(deps, 0)
    labels = [b.text for b in buttons_of(markup)
              if (b.callback_data or "").startswith("r:")]
    assert all(l.startswith("✅") for l in labels)


def test_safe_only_toggle_filters_and_counts(tmp_path):
    from gftrade.store import Store
    from gftrade.discovery.safety import SafetyReport

    store = Store(str(tmp_path / "state.json"))
    unlocked = SafetyReport(mint="x", mint_renounced=True, freeze_none=True,
                            top10_pct=10.0, lp_locked_pct=5.0, standard_token=True)
    verdicts = (make_verdicts(4, safety_ok=True)
                + make_verdicts(6, safety_ok=False, safety=unlocked))
    deps = deps_with(verdicts)
    deps.store = store

    # default: whole badged field shows
    text, _ = scan_page_view(deps, 0)
    assert "Safe-only view" not in text

    # toggled on: only ✅ render, hidden count surfaces
    store.set_setting("scan_safe_only", True)
    text, markup = scan_page_view(deps, 0)
    assert "6 non-✅ hidden" in text
    view_buttons = [b for b in buttons_of(markup)
                    if (b.callback_data or "").startswith("r:")]
    assert len(view_buttons) == 4
    assert all(b.text.startswith("✅") for b in view_buttons)


def test_safe_only_with_nothing_safe_explains_itself(tmp_path):
    from gftrade.store import Store
    from gftrade.discovery.safety import SafetyReport

    store = Store(str(tmp_path / "state.json"))
    store.set_setting("scan_safe_only", True)
    unlocked = SafetyReport(mint="x", mint_renounced=True, freeze_none=True,
                            top10_pct=10.0, lp_locked_pct=5.0, standard_token=True)
    deps = deps_with(make_verdicts(5, safety_ok=False, safety=unlocked))
    deps.store = store
    text, _ = scan_page_view(deps, 0)
    assert "all 5 current candidates" in text and "/settings" in text
