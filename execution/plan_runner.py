#!/usr/bin/env python3
"""
Dry run: load funding_data.json + current open-position state, compute what
the bot would open/close right now, and print it. Places no orders, calls
no venue API — pure read of the repo's own data. Safe to run any time.

Run:  .venv/bin/python3 -m execution.plan_runner [--horizon 3d|7d]
"""

import argparse
from pathlib import Path

from execution.pair_selector import data_age_minutes, is_data_fresh, plan_closes, plan_new_positions
from execution.risk_config import DEFAULT
from execution.state import load_positions

REPO_ROOT = Path(__file__).resolve().parent.parent
FUNDING_DATA_FILE = REPO_ROOT / "funding_data.json"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--horizon", choices=["3d", "7d"], default="7d")
    args = parser.parse_args()

    import json
    funding_data = json.loads(FUNDING_DATA_FILE.read_text())
    open_positions = load_positions()
    risk_config = DEFAULT

    age = data_age_minutes(funding_data)
    fresh = is_data_fresh(funding_data, risk_config)
    print(f"funding_data.json generated_at={funding_data['generated_at']} "
          f"({age:.1f} min ago, {'fresh' if fresh else 'STALE'} — limit {risk_config.max_funding_data_age_minutes}min)")
    print(f"horizon={args.horizon}  open positions={len(open_positions)}/{risk_config.max_concurrent_pairs}  "
          f"capital=${risk_config.total_capital_usd:.0f}\n")

    if open_positions:
        print("Currently open:")
        for pos in open_positions.values():
            print(f"  {pos.symbol}: short {pos.short_venue} / long {pos.long_venue}, "
                  f"${pos.notional_usd:.0f}, opened {pos.opened_at}")
        print()

    closes = plan_closes(funding_data, risk_config, open_positions, args.horizon)
    if closes:
        print(f"Would CLOSE {len(closes)} position(s):")
        for pid in closes:
            pos = open_positions[pid]
            print(f"  {pos.symbol}: short {pos.short_venue} / long {pos.long_venue} (${pos.notional_usd:.0f})")
    else:
        print("Would close: nothing")
    print()

    opens = plan_new_positions(funding_data, risk_config, open_positions, args.horizon)
    if opens:
        print(f"Would OPEN {len(opens)} position(s):")
        for plan in opens:
            print(f"  {plan.symbol}: short {plan.short_venue} / long {plan.long_venue}, "
                  f"${plan.notional_usd:.0f} notional/leg, score={plan.score}, "
                  f"spread={plan.current_spread_apy_pct:.2f}% APY")
    else:
        print("Would open: nothing")


if __name__ == "__main__":
    main()
