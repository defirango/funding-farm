#!/usr/bin/env python3
"""
Paper-trading bot: runs the exact same decision loop a live bot would
(pair_selector.py against funding_data.json + risk_config.py), but
"executes" by recording a simulated position instead of placing a real
order — no key touched, no venue API call, no capital at risk.

This is the bridge between "the dashboard shows opportunities" and "a bot
trades them for real": it proves the full autonomous loop end to end
(data -> selection -> sizing -> simulated fill -> tracking -> live
display) on the same free, zero-maintenance hourly GitHub Actions
schedule the fetcher already uses. Swapping the simulated fill for a real
signed order (risex_client.place_order / a Perpl WS order) is the only
thing that changes once real keys and real capital exist — the decision
loop above it is already this.

P&L model: funding-only, estimated from the dashboard's own hourly
realized-rate history (data/history.jsonl) over each position's actual
holding window — NOT each venue's real per-settlement funding payments,
and it does not model fees, slippage, or price basis between the two
venues at entry/exit. Good enough to show the bot is doing the right
thing directionally; not a substitute for real fill accounting.

Run:  python3 -m execution.paper_bot
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from execution.pair_selector import is_data_fresh, load_funding_data, plan_closes, plan_new_positions
from execution.risk_config import DEFAULT
from execution.state import OpenPosition, load_positions, pair_id, save_positions

REPO_ROOT = Path(__file__).resolve().parent.parent
FUNDING_DATA_FILE = REPO_ROOT / "funding_data.json"
HISTORY_FILE = REPO_ROOT / "data" / "history.jsonl"
PAPER_STATE_FILE = REPO_ROOT / "execution" / "state" / "paper_positions.json"
PAPER_HISTORY_FILE = REPO_ROOT / "execution" / "state" / "paper_history.jsonl"
BOT_STATUS_FILE = REPO_ROOT / "bot_status.json"  # next to funding_data.json — read by index.html

HORIZON = "7d"


def load_history_rows(path=HISTORY_FILE) -> list:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def estimate_funding_pnl_usd(history_rows, symbol, short_venue, long_venue,
                              notional_usd, opened_at_iso, as_of_iso):
    """Sums the realized (short - long) spread across every history snapshot inside
    the holding window and converts the average APY over that window to a dollar
    figure. Returns (pnl_usd, n_observations_used)."""
    opened_at = datetime.fromisoformat(opened_at_iso)
    as_of = datetime.fromisoformat(as_of_iso)
    by_run = {}
    for row in history_rows:
        if row["symbol"] != symbol or row["venue"] not in (short_venue, long_venue):
            continue
        ts = datetime.fromisoformat(row["run_ts"])
        if not (opened_at <= ts <= as_of):
            continue
        by_run.setdefault(row["run_ts"], {})[row["venue"]] = row["apy_pct"]

    spreads = [r[short_venue] - r[long_venue] for r in by_run.values() if short_venue in r and long_venue in r]
    if not spreads:
        return 0.0, 0

    avg_apy_pct = sum(spreads) / len(spreads)
    hold_days = max((as_of - opened_at).total_seconds() / 86400.0, 0.0)
    pnl_usd = notional_usd * (avg_apy_pct / 100.0) * (hold_days / 365.0)
    return pnl_usd, len(spreads)


def append_paper_history(events: list, path=PAPER_HISTORY_FILE) -> None:
    if not events:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")


def load_paper_history(path=PAPER_HISTORY_FILE) -> list:
    if not path.exists():
        return []
    rows = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run():
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    funding_data = load_funding_data(FUNDING_DATA_FILE)
    risk_config = DEFAULT
    positions = load_positions(PAPER_STATE_FILE)
    history_rows = load_history_rows()

    fresh = is_data_fresh(funding_data, risk_config)
    events = []

    close_ids = plan_closes(funding_data, risk_config, positions, HORIZON) if fresh else []
    for pid in close_ids:
        pos = positions.pop(pid)
        pnl_usd, n_obs = estimate_funding_pnl_usd(
            history_rows, pos.symbol, pos.short_venue, pos.long_venue,
            pos.notional_usd, pos.opened_at, now_iso,
        )
        events.append({
            "action": "close", "at": now_iso, "pair_id": pid, "symbol": pos.symbol,
            "short_venue": pos.short_venue, "long_venue": pos.long_venue,
            "notional_usd": pos.notional_usd, "opened_at": pos.opened_at,
            "simulated_pnl_usd": round(pnl_usd, 2), "n_observations": n_obs,
        })

    new_plans = plan_new_positions(funding_data, risk_config, positions, HORIZON) if fresh else []
    for plan in new_plans:
        pid = pair_id(plan.symbol, plan.short_venue, plan.long_venue)
        positions[pid] = OpenPosition(
            pair_id=pid, symbol=plan.symbol, short_venue=plan.short_venue, long_venue=plan.long_venue,
            notional_usd=plan.notional_usd, horizon=plan.horizon, opened_at=now_iso,
        )
        events.append({
            "action": "open", "at": now_iso, "pair_id": pid, "symbol": plan.symbol,
            "short_venue": plan.short_venue, "long_venue": plan.long_venue,
            "notional_usd": plan.notional_usd, "score": plan.score,
            "entry_spread_apy_pct": plan.current_spread_apy_pct,
        })

    save_positions(positions, PAPER_STATE_FILE)
    append_paper_history(events)
    write_bot_status(positions, history_rows, funding_data, risk_config, fresh, now_iso)

    print(f"paper_bot: {len(close_ids)} closed, {len(new_plans)} opened, "
          f"{len(positions)} open now, data_fresh={fresh}")


def write_bot_status(positions, history_rows, funding_data, risk_config, fresh, now_iso):
    open_positions_out = []
    for pos in positions.values():
        unrealized_pnl, n_obs = estimate_funding_pnl_usd(
            history_rows, pos.symbol, pos.short_venue, pos.long_venue,
            pos.notional_usd, pos.opened_at, now_iso,
        )
        hold_hours = (datetime.fromisoformat(now_iso) - datetime.fromisoformat(pos.opened_at)).total_seconds() / 3600.0
        open_positions_out.append({
            "symbol": pos.symbol, "short_venue": pos.short_venue, "long_venue": pos.long_venue,
            "notional_usd": pos.notional_usd, "opened_at": pos.opened_at,
            "hold_hours": round(hold_hours, 1),
            "unrealized_pnl_usd_est": round(unrealized_pnl, 2), "n_observations": n_obs,
        })

    all_events = load_paper_history()
    closed_events = [e for e in all_events if e["action"] == "close"]
    cumulative_realized_pnl = round(sum(e["simulated_pnl_usd"] for e in closed_events), 2)

    status = {
        "mode": "paper",
        "generated_at": now_iso,
        "funding_data_fresh": fresh,
        "capital_usd": risk_config.total_capital_usd,
        "max_concurrent_pairs": risk_config.max_concurrent_pairs,
        "open_positions": open_positions_out,
        "cumulative_realized_pnl_usd": cumulative_realized_pnl,
        "total_closed_trades": len(closed_events),
        "recent_events": all_events[-20:][::-1],
    }
    BOT_STATUS_FILE.write_text(json.dumps(status, indent=2) + "\n")


if __name__ == "__main__":
    run()
