"""Price history / extension gate, vol-scaled exits, factor log, analysis."""
import time

import pytest

from gftrade import config
from gftrade.analysis import compute_report, pearson
from gftrade.discovery.trend import PriceHistory
from gftrade.factors import FactorLog
from gftrade.scanner import Scanner
from gftrade.trading.engine import TradingEngine

from conftest import MINT_A, FakeDex, FakeSafety, make_strong_pair


def seed_history(history, mint, prices, spacing=300, end=None):
    """Backfill a series ending `spacing` seconds ago per step."""
    end = end or time.time()
    start = end - spacing * (len(prices) - 1)
    for i, price in enumerate(prices):
        history.record(mint, price, ts=start + i * spacing)


# ---------- PriceHistory ----------

def test_extension_needs_real_history():
    history = PriceHistory()
    assert history.extension_pct(MINT_A) is None
    history.record(MINT_A, 1.0)
    history.record(MINT_A, 2.0)
    assert history.extension_pct(MINT_A) is None  # 2 points, no span


def test_extension_measures_rise_off_low():
    history = PriceHistory()
    seed_history(history, MINT_A, [1.0, 0.9, 1.1, 1.62])  # low 0.9 -> +80%
    assert history.extension_pct(MINT_A) == pytest.approx(80.0)


def test_volatility_and_pruning():
    history = PriceHistory(window_seconds=3600)
    seed_history(history, MINT_A, [1.0, 1.1, 1.0, 1.1, 1.0])
    vol = history.volatility_pct(MINT_A)
    assert vol is not None and vol > 5  # ~±10% swings
    calm = PriceHistory()
    seed_history(calm, MINT_A, [1.0, 1.001, 1.0, 1.001, 1.0])
    assert calm.volatility_pct(MINT_A) < 1
    stale = PriceHistory(window_seconds=60)
    stale.record(MINT_A, 1.0, ts=time.time() - 3600)
    stale.prune()
    assert stale.extension_pct(MINT_A) is None


# ---------- extension gate in the scanner ----------

def build_scanner_with_history(store, prices_seed):
    dex = FakeDex(pairs_by_mint={MINT_A: make_strong_pair()},
                  profiles=[{"chainId": "solana", "tokenAddress": MINT_A}])
    engine = TradingEngine(store, dex, dry_run=True)
    history = PriceHistory()
    if prices_seed:
        seed_history(history, MINT_A, prices_seed)
    return Scanner(store, dex, engine, FakeSafety(), prices=history)


async def test_extended_coin_blocked_from_alerts_and_autobuy(store):
    store.set_setting("autobuy", True)
    # strong pair price is 0.001; we watched it climb from 0.0005 -> +100%
    scanner = build_scanner_with_history(store, [0.0005, 0.0006, 0.0008, 0.001])
    events = await scanner.tick()
    assert [e for e in events if e["type"] in ("signal", "autobuy")] == []
    assert store.positions == {}


async def test_early_coin_passes_the_gate(store):
    scanner = build_scanner_with_history(store, [0.00095, 0.00097, 0.00096, 0.001])
    events = await scanner.tick()
    signals = [e for e in events if e["type"] == "signal"]
    assert len(signals) == 1
    assert signals[0]["verdict"]["extension_pct"] == pytest.approx(5.26, abs=0.1)


async def test_gate_disabled_or_unknown_history_allows(store):
    store.set_setting("max_entry_extension_pct", 0.0)  # gate off
    scanner = build_scanner_with_history(store, [0.0005, 0.0006, 0.0008, 0.001])
    assert len([e for e in await scanner.tick() if e["type"] == "signal"]) == 1

    fresh_store_signals = build_scanner_with_history  # readability alias
    store.set_setting("max_entry_extension_pct", 60.0)
    store.data["alerts"] = {}
    store.save()
    scanner = fresh_store_signals(store, None)  # no history at all -> unknown
    assert len([e for e in await scanner.tick() if e["type"] == "signal"]) == 1


# ---------- vol-scaled exits ----------

async def test_vol_scaled_exits_widen_and_tighten(store, dex):
    store.set_setting("vol_scaled_exits", True)
    wild = PriceHistory()
    seed_history(wild, MINT_A, [0.001, 0.0011, 0.00095, 0.00108, 0.001])  # swingy
    engine = TradingEngine(store, dex, dry_run=True, price_history=wild)
    result = await engine.buy(MINT_A, 0.1)
    position = result["position"]
    assert position["exit_vol_factor"] > 1.0
    base_tp = 0.001 * (1 + store.settings["take_profit_pct"] / 100)
    assert position["tp_price_usd"] > base_tp  # wider target on a wild coin
    assert position["exit_vol_factor"] <= config.VOL_FACTOR_MAX


async def test_vol_scaling_off_or_no_history_uses_flat(store, dex, engine):
    result = await engine.buy(MINT_A, 0.1)  # engine fixture has no history
    assert result["position"]["exit_vol_factor"] == 1.0
    assert result["position"]["tp_price_usd"] == pytest.approx(
        0.001 * (1 + store.settings["take_profit_pct"] / 100))


# ---------- factor log ----------

def make_verdict(mint=MINT_A, score=80, screened=True):
    return {
        "pair": make_strong_pair(mint=mint), "mint": mint,
        "screened_ok": screened, "safety_ok": screened, "safety": None,
        "patterns": [{"pattern": "volume_surge", "confidence": 0.7}],
        "score": score, "breakdown": {}, "reject_reasons": [],
        "extension_pct": 12.0,
    }


@pytest.fixture
def factor_log(tmp_path):
    log = FactorLog(str(tmp_path / "factors.db"))
    yield log
    log.close()


def test_snapshot_logging_and_dedupe(factor_log):
    row_id = factor_log.log_snapshot(make_verdict())
    assert row_id is not None
    assert factor_log.log_snapshot(make_verdict()) == row_id  # deduped
    assert factor_log.log_snapshot(make_verdict(), dedupe_minutes=0) != row_id
    row = factor_log.all_rows()[0]
    assert row["score"] == 80 and row["screened_ok"] == 1
    assert row["extension_pct"] == 12.0
    assert row["liq_mcap_ratio"] == pytest.approx(60_000 / 400_000)


def test_trade_outcome_attaches(factor_log):
    row_id = factor_log.log_snapshot(make_verdict())
    assert factor_log.latest_id_for_mint(MINT_A) == row_id
    factor_log.update_trade_outcome(row_id, "take_profit", 35.0)
    row = factor_log.all_rows()[0]
    assert row["trade_result"] == "take_profit"
    assert row["trade_pnl_pct"] == 35.0
    factor_log.update_trade_outcome(None, "x", 0)  # no-op, no crash


def test_checkpoints_fill_and_dead_tokens(factor_log):
    row_id = factor_log.log_snapshot(make_verdict())
    # backdate the snapshot 7h: h1 and h6 due, h24 not yet
    factor_log._db.execute("UPDATE snapshots SET ts = ts - 25200 WHERE id = ?",
                           (row_id,))
    factor_log._db.commit()
    assert MINT_A in factor_log.due_checkpoint_mints()
    factor_log.fill_checkpoints({MINT_A: 0.002})
    row = factor_log.all_rows()[0]
    assert row["p_h1"] == 0.002 and row["p_h6"] == 0.002 and row["p_h24"] is None
    # a vanished market past grace records 0
    row2 = factor_log.log_snapshot(make_verdict(mint="B" * 40 + "bbbb"),
                                   dedupe_minutes=0)
    factor_log._db.execute("UPDATE snapshots SET ts = ts - 14400 WHERE id = ?",
                           (row2,))
    factor_log._db.commit()
    factor_log.fill_checkpoints({})
    assert factor_log.all_rows()[1]["p_h1"] == 0


async def test_engine_closes_the_factor_loop(store, dex, factor_log):
    factor_log.log_snapshot(make_verdict())
    engine = TradingEngine(store, dex, dry_run=True, factors=factor_log)
    await engine.buy(MINT_A, 0.1)
    from conftest import make_pair
    dex.pairs_by_mint[MINT_A] = make_pair(price_usd=0.002)
    await engine.sell(MINT_A, 100, reason="manual")
    row = factor_log.all_rows()[0]
    assert row["trade_result"] == "manual"
    assert row["trade_pnl_pct"] > 0


# ---------- analysis ----------

def test_pearson_basics():
    assert pearson([1, 2, 3], [2, 4, 6]) == pytest.approx(1.0)
    assert pearson([1, 2, 3], [6, 4, 2]) == pytest.approx(-1.0)
    assert pearson([1, 1, 1], [1, 2, 3]) is None  # zero variance
    assert pearson([1, 2], [1, 2]) is None        # too few


def test_report_ranks_a_predictive_factor(factor_log):
    # score perfectly predicts the 24h outcome; liquidity is constant noise
    for i in range(30):
        verdict = make_verdict(mint=f"F{i:02d}" + "z" * 38, score=40 + i * 2)
        row_id = factor_log.log_snapshot(verdict, dedupe_minutes=0)
        factor_log._db.execute(
            "UPDATE snapshots SET p_h24 = price0 * ?, ts = ts - 90000 WHERE id = ?",
            (0.5 + i * 0.05, row_id),
        )
    factor_log._db.commit()
    report = compute_report(factor_log.all_rows())
    lines = [l for l in report.splitlines() if l.strip().startswith("score")]
    assert lines and "+1.00" in lines[0]
    assert "Correlation is NOT causation" in report
    assert "Small sample" not in report  # 30 resolved rows


def test_report_warns_on_tiny_sample(factor_log):
    row_id = factor_log.log_snapshot(make_verdict())
    factor_log._db.execute(
        "UPDATE snapshots SET p_h24 = price0 * 2 WHERE id = ?", (row_id,))
    factor_log._db.commit()
    report = compute_report(factor_log.all_rows())
    assert "Small sample" in report
    assert compute_report([]).count("Nothing to analyze") == 1
