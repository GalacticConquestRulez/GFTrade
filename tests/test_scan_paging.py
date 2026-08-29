"""Paged /scan view: best ranked first, arrows to move through pages."""
import time
from types import SimpleNamespace

from gftrade.tg.handlers import SCAN_PAGE_SIZE, scan_page_view

from conftest import make_pair


def make_verdicts(count):
    verdicts = []
    for i in range(count):
        mint = chr(ord("C") + i) * 40 + "zzzz"
        pair = make_pair(mint=mint, symbol=f"TK{i}")
        verdicts.append({
            "pair": pair, "mint": mint, "score": 95 - i, "breakdown": {},
            "patterns": [{"pattern": "volume_surge", "confidence": 0.7}],
            "safety": None, "screened_ok": True, "reject_reasons": [],
        })
    return verdicts


def deps_with(verdicts):
    return SimpleNamespace(scanner=SimpleNamespace(
        last_scan={"verdicts": verdicts, "at": time.time()}
    ))


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
    assert "nothing currently passes" in text
    callbacks = [b.callback_data for b in buttons_of(markup)]
    assert "scan" in callbacks  # re-scan stays available
    assert not any((c or "").startswith("scp:") for c in callbacks)
