import unittest
from datetime import datetime, timedelta, timezone

from execution.pair_selector import (
    automatable_candidates,
    is_data_fresh,
    plan_closes,
    plan_new_positions,
)
from execution.risk_config import RiskConfig
from execution.state import OpenPosition, pair_id


def _pair(symbol, short_venue, long_venue, score=90.0, spread=20.0, confident=True):
    return {
        "symbol": symbol, "short_venue": short_venue, "long_venue": long_venue,
        "current_spread_apy_pct": spread, "realized_avg_apy_pct": spread * 0.9,
        "short_rate_apy_pct": spread, "long_rate_apy_pct": 0.0,
        "score": score, "n_observations": 72, "confident": confident, "hold_days": 7,
    }


def _funding_data(pairs_7d, generated_at=None):
    generated_at = generated_at or datetime.now(timezone.utc)
    return {
        "generated_at": generated_at.isoformat(),
        "venues": [{"key": k, "label": k} for k in ("variational", "risex", "perpl")],
        "symbols_seen": len(pairs_7d),
        "history_runs": 100,
        "pairs": {"3d": [], "7d": pairs_7d},
    }


class FreshnessTest(unittest.TestCase):
    def test_fresh_within_window(self):
        fd = _funding_data([], generated_at=datetime.now(timezone.utc) - timedelta(minutes=10))
        self.assertTrue(is_data_fresh(fd, RiskConfig()))

    def test_stale_beyond_window(self):
        fd = _funding_data([], generated_at=datetime.now(timezone.utc) - timedelta(minutes=200))
        self.assertFalse(is_data_fresh(fd, RiskConfig(max_funding_data_age_minutes=90)))


class AutomatableCandidatesTest(unittest.TestCase):
    def test_excludes_variational_legs(self):
        pairs = [
            _pair("XRP", "risex", "variational", score=99),
            _pair("PUMP", "risex", "perpl", score=95),
        ]
        fd = _funding_data(pairs)
        out = automatable_candidates(fd, RiskConfig(), "7d")
        self.assertEqual([p["symbol"] for p in out], ["PUMP"])

    def test_excludes_low_score_and_unconfident(self):
        pairs = [
            _pair("A", "risex", "perpl", score=10),
            _pair("B", "risex", "perpl", score=90, confident=False),
            _pair("C", "risex", "perpl", score=90, confident=True),
        ]
        fd = _funding_data(pairs)
        out = automatable_candidates(fd, RiskConfig(min_score_to_open=50, require_confident_flag=True), "7d")
        self.assertEqual([p["symbol"] for p in out], ["C"])

    def test_sorted_best_first(self):
        pairs = [
            _pair("LOW", "risex", "perpl", score=60),
            _pair("HIGH", "risex", "perpl", score=95),
        ]
        fd = _funding_data(pairs)
        out = automatable_candidates(fd, RiskConfig(min_score_to_open=0), "7d")
        self.assertEqual([p["symbol"] for p in out], ["HIGH", "LOW"])


class PlanNewPositionsTest(unittest.TestCase):
    def test_stale_data_opens_nothing(self):
        fd = _funding_data([_pair("A", "risex", "perpl")], generated_at=datetime.now(timezone.utc) - timedelta(hours=5))
        self.assertEqual(plan_new_positions(fd, RiskConfig(), {}), [])

    def test_respects_free_slots(self):
        pairs = [_pair(sym, "risex", "perpl", score=90 - i) for i, sym in enumerate(["A", "B", "C"])]
        fd = _funding_data(pairs)
        plans = plan_new_positions(fd, RiskConfig(max_concurrent_pairs=2), {})
        self.assertEqual(len(plans), 2)
        self.assertEqual([p.symbol for p in plans], ["A", "B"])

    def test_skips_symbol_already_open(self):
        pairs = [_pair("A", "risex", "perpl", score=95), _pair("B", "risex", "perpl", score=90)]
        fd = _funding_data(pairs)
        open_positions = {
            pair_id("A", "risex", "perpl"): OpenPosition(
                pair_id=pair_id("A", "risex", "perpl"), symbol="A", short_venue="risex",
                long_venue="perpl", notional_usd=200, horizon="7d", opened_at="2026-09-01T00:00:00+00:00",
            )
        }
        plans = plan_new_positions(fd, RiskConfig(max_concurrent_pairs=2), open_positions)
        self.assertEqual([p.symbol for p in plans], ["B"])

    def test_caps_notional_at_remaining_capital(self):
        pairs = [_pair("A", "risex", "perpl")]
        fd = _funding_data(pairs)
        config = RiskConfig(total_capital_usd=250, max_notional_per_trade_usd=200, max_concurrent_pairs=2)
        open_positions = {
            pair_id("X", "risex", "perpl"): OpenPosition(
                pair_id=pair_id("X", "risex", "perpl"), symbol="X", short_venue="risex",
                long_venue="perpl", notional_usd=100, horizon="7d", opened_at="2026-09-01T00:00:00+00:00",
            )
        }
        plans = plan_new_positions(fd, config, open_positions)
        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].notional_usd, 150)  # 250 total - 100 already deployed


class PlanClosesTest(unittest.TestCase):
    def _open(self, symbol, short_venue="risex", long_venue="perpl"):
        pid = pair_id(symbol, short_venue, long_venue)
        return pid, OpenPosition(pair_id=pid, symbol=symbol, short_venue=short_venue, long_venue=long_venue,
                                  notional_usd=200, horizon="7d", opened_at="2026-09-01T00:00:00+00:00")

    def test_stale_data_closes_nothing(self):
        pid, pos = self._open("A")
        fd = _funding_data([_pair("A", "risex", "perpl", spread=-5)],
                            generated_at=datetime.now(timezone.utc) - timedelta(hours=5))
        self.assertEqual(plan_closes(fd, RiskConfig(), {pid: pos}), [])

    def test_closes_on_sign_flip(self):
        pid, pos = self._open("A")
        fd = _funding_data([_pair("A", "risex", "perpl", spread=-5, score=90)])
        self.assertEqual(plan_closes(fd, RiskConfig(), {pid: pos}), [pid])

    def test_closes_on_score_decay(self):
        pid, pos = self._open("A")
        fd = _funding_data([_pair("A", "risex", "perpl", spread=5, score=10)])
        self.assertEqual(plan_closes(fd, RiskConfig(min_score_to_open=50), {pid: pos}), [pid])

    def test_closes_when_pair_disappears(self):
        pid, pos = self._open("A")
        fd = _funding_data([_pair("B", "risex", "perpl", spread=5, score=90)])
        self.assertEqual(plan_closes(fd, RiskConfig(), {pid: pos}), [pid])

    def test_keeps_healthy_position_open(self):
        pid, pos = self._open("A")
        fd = _funding_data([_pair("A", "risex", "perpl", spread=25, score=90)])
        self.assertEqual(plan_closes(fd, RiskConfig(min_score_to_open=50), {pid: pos}), [])


if __name__ == "__main__":
    unittest.main()
