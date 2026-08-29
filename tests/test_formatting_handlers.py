import pytest

from gftrade.tg import formatting as fmt
from gftrade.tg.handlers import _parse_setting, extract_mint

from conftest import GOOD_SAFETY, MINT_A, make_pair, make_strong_pair


def test_token_card_escapes_hostile_metadata():
    pair = make_pair(symbol="<b>PWN</b>")
    pair["baseToken"]["name"] = "<script>alert(1)</script>"
    card = fmt.token_card(pair, GOOD_SAFETY, 75, {"momentum": 20}, [])
    assert "<b>PWN</b></b>" not in card
    assert "&lt;b&gt;PWN&lt;/b&gt;" in card
    assert "<script>" not in card


def test_signal_and_receipt_cards_render():
    verdict = {
        "pair": make_strong_pair(), "mint": MINT_A, "score": 85,
        "breakdown": {"momentum": 20.0, "volume": 15.0}, "safety": GOOD_SAFETY,
        "patterns": [{"pattern": "volume_surge", "confidence": 0.8}],
        "screened_ok": True, "reject_reasons": [],
    }
    card = fmt.signal_card(verdict)
    assert "New signal" in card and "85" in card

    position = {
        "mint": MINT_A, "symbol": "MOON", "sol_spent": 0.5, "token_amount": 12345.0,
        "entry_price_usd": 0.001, "tp_price_usd": 0.0013, "sl_price_usd": 0.00085,
        "dry_run": True,
    }
    receipt = fmt.buy_receipt({"position": position, "price_usd": 0.001,
                               "signature": None, "merged": False})
    assert "MOON" in receipt and "DRY RUN" in receipt


def test_price_formatting_handles_tiny_values():
    assert fmt.fmt_price(0.000004521).startswith("$0.0000045")
    assert fmt.fmt_price(1234.5) == "$1,234.5000"
    assert fmt.fmt_usd(2_500_000) == "$2.50M"
    assert fmt.fmt_usd(13_400) == "$13.40k"


def test_positions_text_smoke(tmp_path):
    positions = {
        MINT_A: {
            "symbol": "TEST", "dry_run": True, "source": "manual",
            "entry_price_usd": 0.001, "token_amount": 1000.0, "sol_spent": 0.5,
            "sol_received": 0.1, "tp_price_usd": 0.0013, "sl_price_usd": 0.00085,
            "peak_price_usd": 0.0011,
        }
    }
    text = fmt.positions_text(positions, {MINT_A: make_pair(price_usd=0.0011)}, 200.0)
    assert "TEST" in text and "recovered" in text
    assert fmt.positions_text({}, {}, 0) == "No open positions."


def test_extract_mint_validates_base58():
    text = f"check this out {MINT_A} looks good"
    assert extract_mint(text) == MINT_A
    assert extract_mint("hello world") is None
    assert extract_mint("0" * 44) is None          # 0 is not a base58 char
    assert extract_mint("abc") is None


def test_parse_setting_slippage_percent_to_bps():
    assert _parse_setting("slippage_bps", "2") == 200
    assert _parse_setting("slippage_bps", "0.5%") == 50
    with pytest.raises(ValueError):
        _parse_setting("slippage_bps", "90")


def test_parse_setting_presets_and_bounds():
    assert _parse_setting("buy_presets", "0.1, 0.5, 1") == [0.1, 0.5, 1.0]
    with pytest.raises(ValueError):
        _parse_setting("buy_presets", "1,2,3,4")
    with pytest.raises(ValueError):
        _parse_setting("max_positions", "0")
    assert _parse_setting("max_positions", "5") == 5
    assert _parse_setting("trailing_stop_pct", "0") == 0
    with pytest.raises(ValueError):
        _parse_setting("stop_loss_pct", "0")
    assert _parse_setting("min_alert_score", "75") == 75
    with pytest.raises(ValueError):
        _parse_setting("min_alert_score", "150")
