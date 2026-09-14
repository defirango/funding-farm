"""
Persisted open-position state. In the serverless/git-as-database
architecture, this file gets committed back to the repo by the workflow
after each run — same pattern data/history.jsonl already uses for the
dashboard's fetcher.
"""

import json
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_STATE_FILE = Path(__file__).resolve().parent / "state" / "open_positions.json"


@dataclass
class OpenPosition:
    pair_id: str
    symbol: str
    short_venue: str
    long_venue: str
    notional_usd: float
    horizon: str
    opened_at: str  # ISO 8601 UTC
    short_order_ref: str = ""
    long_order_ref: str = ""


def pair_id(symbol: str, short_venue: str, long_venue: str) -> str:
    return f"{symbol}|{short_venue}>{long_venue}"


def load_positions(path=DEFAULT_STATE_FILE) -> dict:
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    return {k: OpenPosition(**v) for k, v in raw.items()}


def save_positions(positions: dict, path=DEFAULT_STATE_FILE) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: asdict(v) for k, v in positions.items()}, indent=2) + "\n")
