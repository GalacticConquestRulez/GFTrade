import pytest

from gftrade import config
from gftrade.store import Store


def test_defaults_seeded_and_roundtrip(tmp_path):
    path = str(tmp_path / "state.json")
    store = Store(path)
    assert store.settings == config.DEFAULT_SETTINGS
    store.set_setting("slippage_bps", 350)
    store.mute("SOMEMINT")
    store.record_alert("ALERTED")

    reloaded = Store(path)
    assert reloaded.settings["slippage_bps"] == 350
    assert reloaded.is_muted("SOMEMINT")
    assert reloaded.recently_alerted("ALERTED")
    assert not reloaded.recently_alerted("NEVER")


def test_unknown_setting_rejected(tmp_path):
    store = Store(str(tmp_path / "state.json"))
    with pytest.raises(KeyError):
        store.set_setting("not_a_setting", 1)


def test_new_default_settings_merge_into_old_state(tmp_path):
    path = str(tmp_path / "state.json")
    store = Store(path)
    del store.data["settings"]["trailing_stop_pct"]
    store.save()
    reloaded = Store(path)
    assert reloaded.settings["trailing_stop_pct"] == config.DEFAULT_SETTINGS["trailing_stop_pct"]


def test_close_position_accumulates_realized_pnl(tmp_path):
    store = Store(str(tmp_path / "state.json"))
    store.put_position({"mint": "M1", "symbol": "X"})
    store.close_position("M1", {"mint": "M1", "pnl_sol": 0.25})
    store.put_position({"mint": "M2", "symbol": "Y"})
    store.close_position("M2", {"mint": "M2", "pnl_sol": -0.1})
    assert store.stats["realized_pnl_sol"] == pytest.approx(0.15)
    assert store.summary()["closed_trades"] == 2
    assert store.summary()["win_rate"] == pytest.approx(0.5)


def test_sim_balance_adjustments_persist(tmp_path):
    path = str(tmp_path / "state.json")
    store = Store(path)
    store.sim_adjust_balance(-0.4)
    store.sim_adjust_balance(0.1)
    assert Store(path).stats["sim_balance_sol"] == pytest.approx(
        config.SIM_START_BALANCE_SOL - 0.3
    )


def test_requested_exit_defaults_are_pinned():
    """Auto-sell levels requested by the owners: +35% TP, -30% SL."""
    assert config.DEFAULT_SETTINGS["take_profit_pct"] == 35.0
    assert config.DEFAULT_SETTINGS["stop_loss_pct"] == 30.0
