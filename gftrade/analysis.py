"""
Factor analysis over the SQLite factor log: which of the things we measure
at evaluation time actually correlate with what happened afterward?

Two outcome definitions, reported side by side:
- 24h return of EVERY logged snapshot ((p_h24 - price0) / price0; a
  vanished market = -100%) — the big sample.
- Real trade results (win = pnl > 0) — small sample, but it's the one
  that includes exit mechanics.

Pearson correlation only, computed with stdlib — the point is ranking
signal strength, not econometrics. Read the output with both caveats it
prints: correlation is not causation, and small samples lie.

Run standalone: python -m gftrade.analysis   (or /factors in Telegram)
"""
import statistics

from .factors import FACTOR_COLUMNS, FactorLog

MIN_SAMPLE_WARN = 20


def pearson(xs: list, ys: list):
    """Pearson r, or None when undefined (n<3 or zero variance)."""
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x, mean_y = statistics.fmean(xs), statistics.fmean(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    var_x = sum(d * d for d in dx)
    var_y = sum(d * d for d in dy)
    if var_x == 0 or var_y == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / (var_x ** 0.5 * var_y ** 0.5)


def _paired(rows: list, factor: str, outcome_fn) -> tuple:
    xs, ys = [], []
    for row in rows:
        x = row.get(factor)
        y = outcome_fn(row)
        if x is None or y is None:
            continue
        xs.append(float(x))
        ys.append(float(y))
    return xs, ys


def _ret24(row):
    p24, p0 = row.get("p_h24"), row.get("price0")
    if p24 is None or not p0:
        return None
    return (p24 - p0) / p0 * 100


def _win(row):
    pnl = row.get("trade_pnl_pct")
    if row.get("trade_result") is None or pnl is None:
        return None
    return 1.0 if pnl > 0 else 0.0


def compute_report(rows: list) -> str:
    resolved = [r for r in rows if _ret24(r) is not None]
    traded = [r for r in rows if _win(r) is not None]

    lines = ["📊 Factor analysis"]
    lines.append(f"Snapshots: {len(rows)} logged · {len(resolved)} with a 24h "
                 f"outcome · {len(traded)} actually traded")
    if not resolved and not traded:
        lines.append("\nNothing to analyze yet — outcomes start resolving "
                     "~1h after snapshots are logged.")
        return "\n".join(lines)
    if len(resolved) < MIN_SAMPLE_WARN and len(traded) < MIN_SAMPLE_WARN:
        lines.append(f"⚠️ Small sample (<{MIN_SAMPLE_WARN}) — treat every "
                     "number below as noise until this grows.")

    ranked = []
    for factor in FACTOR_COLUMNS:
        xs, ys = _paired(resolved, factor, _ret24)
        r_ret = pearson(xs, ys)
        xs, ys = _paired(traded, factor, _win)
        r_win = pearson(xs, ys)
        if r_ret is None and r_win is None:
            continue
        ranked.append((factor, r_ret, r_win, len(xs)))
    ranked.sort(key=lambda item: -abs(item[1] if item[1] is not None else 0))

    lines.append("\nfactor | r vs 24h return | r vs trade win")
    for factor, r_ret, r_win, _ in ranked:
        fmt_ret = f"{r_ret:+.2f}" if r_ret is not None else "  — "
        fmt_win = f"{r_win:+.2f}" if r_win is not None else "  — "
        lines.append(f"{factor:<18} {fmt_ret:>7}          {fmt_win:>7}")

    if traded:
        wins = [r for r in traded if _win(r) == 1.0]
        losses = [r for r in traded if _win(r) == 0.0]
        if wins and losses:
            lines.append("\nAverage at entry, wins vs losses:")
            for factor in FACTOR_COLUMNS:
                win_vals = [r[factor] for r in wins if r.get(factor) is not None]
                loss_vals = [r[factor] for r in losses if r.get(factor) is not None]
                if win_vals and loss_vals:
                    lines.append(
                        f"{factor:<18} win {statistics.fmean(win_vals):>10.2f}"
                        f" · loss {statistics.fmean(loss_vals):>10.2f}"
                    )

    lines.append("\nCorrelation is NOT causation — a factor can rank high "
                 "because of what it coincides with, not what it causes. Use "
                 "this to pick which thresholds to experiment with, not as "
                 "proof.")
    return "\n".join(lines)


def main() -> None:
    log = FactorLog()
    try:
        print(compute_report(log.all_rows()))
    finally:
        log.close()


if __name__ == "__main__":
    main()
