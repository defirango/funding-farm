from dataclasses import dataclass


@dataclass(frozen=True)
class RiskConfig:
    # Sized around a $1000 account. imad's numbers from the project brief —
    # these are starting points, not tuned; change only on explicit instruction.
    total_capital_usd: float = 1000.0
    max_notional_per_trade_usd: float = 200.0
    max_concurrent_pairs: int = 2
    max_leverage: float = 3.0
    min_liquidation_buffer_pct: float = 0.35
    liquidation_warning_pct: float = 0.15
    max_funding_data_age_minutes: int = 90
    min_score_to_open: float = 50.0
    require_confident_flag: bool = True

    # Only these two venues have a trading API right now — Variational stays
    # a manual leg, so a pair involving it can never be auto-executed.
    automatable_venues: frozenset = frozenset({"risex", "perpl"})


DEFAULT = RiskConfig()
