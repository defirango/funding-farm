"""
Turns funding_data.json + risk_config + current open positions into a plan:
which new pairs to open, how big, and which open pairs look ready to close.

Pure logic, read-only — this never places an order itself. plan_runner.py
is the dry-run CLI that exercises this against real data and prints what it
would do. A first pass at sizing, deliberately simple (flat per-trade cap,
split evenly across open slots) — meant to be tuned once real behavior is
visible, not a final allocator.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from execution.risk_config import RiskConfig


@dataclass
class TradePlan:
    symbol: str
    short_venue: str
    long_venue: str
    notional_usd: float
    horizon: str
    score: float
    current_spread_apy_pct: float


def load_funding_data(path) -> dict:
    return json.loads(Path(path).read_text())


def data_age_minutes(funding_data: dict) -> float:
    generated_at = datetime.fromisoformat(funding_data["generated_at"])
    return (datetime.now(timezone.utc) - generated_at).total_seconds() / 60.0


def is_data_fresh(funding_data: dict, risk_config: RiskConfig) -> bool:
    return data_age_minutes(funding_data) <= risk_config.max_funding_data_age_minutes


def automatable_candidates(funding_data: dict, risk_config: RiskConfig, horizon: str) -> list:
    """Pairs where BOTH legs are on a venue we can actually trade, meeting the score/confidence bar, best first."""
    pairs = funding_data.get("pairs", {}).get(horizon, [])
    out = []
    for p in pairs:
        if p["short_venue"] not in risk_config.automatable_venues:
            continue
        if p["long_venue"] not in risk_config.automatable_venues:
            continue
        if p["score"] < risk_config.min_score_to_open:
            continue
        if risk_config.require_confident_flag and not p.get("confident", False):
            continue
        out.append(p)
    out.sort(key=lambda p: p["score"], reverse=True)
    return out


def plan_new_positions(funding_data: dict, risk_config: RiskConfig, open_positions: dict,
                        horizon: str = "7d") -> list:
    if not is_data_fresh(funding_data, risk_config):
        return []  # stale data: open nothing new rather than act on a guess

    open_symbols = {pos.symbol for pos in open_positions.values()}
    deployed_usd = sum(pos.notional_usd for pos in open_positions.values())
    free_slots = risk_config.max_concurrent_pairs - len(open_positions)
    if free_slots <= 0:
        return []

    plans = []
    for p in automatable_candidates(funding_data, risk_config, horizon):
        if len(plans) >= free_slots:
            break
        if p["symbol"] in open_symbols:  # don't double up on a symbol already open elsewhere
            continue
        notional = min(
            risk_config.max_notional_per_trade_usd,
            risk_config.total_capital_usd - deployed_usd,
        )
        if notional <= 0:
            break
        plans.append(TradePlan(
            symbol=p["symbol"], short_venue=p["short_venue"], long_venue=p["long_venue"],
            notional_usd=notional, horizon=horizon,
            score=p["score"], current_spread_apy_pct=p["current_spread_apy_pct"],
        ))
        deployed_usd += notional
    return plans


def plan_closes(funding_data: dict, risk_config: RiskConfig, open_positions: dict,
                 horizon: str = "7d") -> list:
    """
    Conservative v1: close a position if fresh data says its edge has
    flipped sign (now costing funding, not earning it), if it's fallen well
    below the bar that would open it fresh today, or if it's dropped out of
    the top-N entirely (the fetcher only publishes the top 20 — this can't
    tell "rank 21" apart from "gone", so it treats both as "can't currently
    justify holding"; that's a real simplification worth revisiting if it
    causes needless churn). If data is stale, close nothing — acting on a
    guess is its own risk, and a stale-data fail-safe belongs at the runner
    level (which should stop opening new ones), not baked in here as "sell
    everything blind".
    """
    if not is_data_fresh(funding_data, risk_config):
        return []

    by_symbol = {}
    for p in funding_data.get("pairs", {}).get(horizon, []):
        by_symbol.setdefault(p["symbol"], []).append(p)

    to_close = []
    for pid, pos in open_positions.items():
        current = next(
            (p for p in by_symbol.get(pos.symbol, [])
             if p["short_venue"] == pos.short_venue and p["long_venue"] == pos.long_venue),
            None,
        )
        if current is None or current["current_spread_apy_pct"] <= 0 \
                or current["score"] < risk_config.min_score_to_open * 0.5:
            to_close.append(pid)
    return to_close
