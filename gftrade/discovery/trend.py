"""
Rolling per-token price history, built from the scanner's own polling —
DexScreener exposes no OHLCV, so the only way to know a coin's recent path
is to remember what we saw. Powers two things:

- The trend-stage (extension) gate: "how far above its own recent low is
  this price?" A coin +80% off its 1h low is late in a move — entering
  there is how you buy the top and ride it through your stop. (The exact
  failure this bot once logged: entered after a +35% pump, exit filled at
  -47.8%.)
- Volatility-scaled exits: a coin that swings 8% between polls shouldn't
  share stop distances with one that swings 1%.

In-memory only: history rebuilds within ~2 discovery ticks of a restart,
and both consumers treat "not enough data" as unknown, never as a guess.
"""
import statistics
import time
from collections import deque


class PriceHistory:
    def __init__(self, window_seconds: int = 2 * 3600, max_points: int = 240):
        self.window_seconds = window_seconds
        self.max_points = max_points
        self._series = {}  # mint -> deque[(ts, price)]

    def record(self, mint: str, price: float, ts: float = None) -> None:
        if not mint or not price or price <= 0:
            return
        ts = ts if ts is not None else time.time()
        series = self._series.setdefault(mint, deque(maxlen=self.max_points))
        series.append((ts, float(price)))

    def _window(self, mint: str, lookback_seconds: int) -> list:
        cutoff = time.time() - lookback_seconds
        return [(ts, p) for ts, p in self._series.get(mint, ()) if ts >= cutoff]

    def prune(self) -> None:
        cutoff = time.time() - self.window_seconds
        for mint in list(self._series):
            series = self._series[mint]
            while series and series[0][0] < cutoff:
                series.popleft()
            if not series:
                del self._series[mint]

    def extension_pct(self, mint: str, lookback_seconds: int = 3600,
                      min_points: int = 3, min_span_seconds: int = 600):
        """How far the latest observed price sits above the lowest price we
        saw in the lookback window, in percent. None until the buffer holds
        at least `min_points` spanning `min_span_seconds` — a judgment
        about trend stage needs actual history, not two glances."""
        points = self._window(mint, lookback_seconds)
        if len(points) < min_points or points[-1][0] - points[0][0] < min_span_seconds:
            return None
        low = min(p for _, p in points)
        if low <= 0:
            return None
        return (points[-1][1] / low - 1) * 100

    def volatility_pct(self, mint: str, lookback_seconds: int = 3600,
                       min_points: int = 4):
        """Stdev of point-to-point percent moves in the window — a rough
        per-poll volatility. None when the buffer is too thin."""
        points = self._window(mint, lookback_seconds)
        if len(points) < min_points:
            return None
        moves = []
        for (_, prev), (_, cur) in zip(points, points[1:]):
            if prev > 0:
                moves.append((cur / prev - 1) * 100)
        if len(moves) < min_points - 1:
            return None
        return statistics.pstdev(moves)
