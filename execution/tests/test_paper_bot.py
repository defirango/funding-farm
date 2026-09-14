import unittest
from datetime import datetime, timedelta, timezone

from execution.paper_bot import estimate_funding_pnl_usd


def _row(run_ts, venue, symbol, apy_pct):
    return {"run_ts": run_ts, "venue": venue, "symbol": symbol, "apy_pct": apy_pct}


class EstimateFundingPnlTest(unittest.TestCase):
    def test_no_matching_rows_returns_zero(self):
        pnl, n = estimate_funding_pnl_usd([], "A", "risex", "perpl", 200,
                                           "2026-09-01T00:00:00+00:00", "2026-09-02T00:00:00+00:00")
        self.assertEqual((pnl, n), (0.0, 0))

    def test_constant_spread_over_one_year_equals_notional_times_rate(self):
        opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
        closed = opened + timedelta(days=365)
        rows = []
        t = opened
        while t <= closed:
            rows.append(_row(t.isoformat(), "risex", "A", 30.0))
            rows.append(_row(t.isoformat(), "perpl", "A", 10.0))  # constant 20% spread
            t += timedelta(hours=6)

        pnl, n = estimate_funding_pnl_usd(rows, "A", "risex", "perpl", 1000,
                                           opened.isoformat(), closed.isoformat())
        self.assertAlmostEqual(pnl, 200.0, delta=1.0)  # 1000 * 20% * (365/365)
        self.assertGreater(n, 0)

    def test_ignores_rows_outside_holding_window(self):
        opened = datetime(2026, 1, 10, tzinfo=timezone.utc)
        closed = datetime(2026, 1, 11, tzinfo=timezone.utc)
        rows = [
            _row("2026-01-05T00:00:00+00:00", "risex", "A", 100.0),  # before window
            _row("2026-01-05T00:00:00+00:00", "perpl", "A", 0.0),
            _row("2026-01-15T00:00:00+00:00", "risex", "A", 100.0),  # after window
            _row("2026-01-15T00:00:00+00:00", "perpl", "A", 0.0),
            _row("2026-01-10T12:00:00+00:00", "risex", "A", 10.0),  # inside window
            _row("2026-01-10T12:00:00+00:00", "perpl", "A", 0.0),
        ]
        pnl, n = estimate_funding_pnl_usd(rows, "A", "risex", "perpl", 1000,
                                           opened.isoformat(), closed.isoformat())
        self.assertEqual(n, 1)
        self.assertGreater(pnl, 0)
        self.assertLess(pnl, 1000 * 1.0)  # nowhere near the 100%-apy rows outside the window

    def test_ignores_other_symbols_and_venues(self):
        opened = datetime(2026, 1, 1, tzinfo=timezone.utc)
        closed = datetime(2026, 1, 2, tzinfo=timezone.utc)
        rows = [
            _row("2026-01-01T12:00:00+00:00", "variational", "A", 999.0),  # not a leg of this pair
            _row("2026-01-01T12:00:00+00:00", "risex", "B", 999.0),  # wrong symbol
            _row("2026-01-01T12:00:00+00:00", "risex", "A", 20.0),
            _row("2026-01-01T12:00:00+00:00", "perpl", "A", 5.0),
        ]
        pnl, n = estimate_funding_pnl_usd(rows, "A", "risex", "perpl", 1000,
                                           opened.isoformat(), closed.isoformat())
        self.assertEqual(n, 1)
        expected = 1000 * (0.15) * (1.0 / 365.0)
        self.assertAlmostEqual(pnl, expected, places=4)


if __name__ == "__main__":
    unittest.main()
