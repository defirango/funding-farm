#!/usr/bin/env python3
"""
fetcher.py — polls Variational, RiseX, and Perpl for funding-rate data,
builds a rolling history in data/history.jsonl, computes 3D/7D realized
funding spreads between every pair of venues for every symbol that trades
on 2+ of them, scores each pair, and writes funding_data.json (repo root)
for the dashboard (index.html) to read.

This runs inside a GitHub Actions workflow on a schedule (every 4h) — see
.github/workflows/update-funding.yml and README.md for setup. The workflow
commits the updated funding_data.json / data/history.jsonl back to the
repo, which triggers Vercel to redeploy the static site automatically.
No third-party dependencies: stdlib only (urllib, json).

────────────────────────────────────────────────────────────────────────
IMPORTANT — read this before you trust the numbers
────────────────────────────────────────────────────────────────────────
This was written from public API docs without the ability to make live
network calls to these venues (the build sandbox's egress is locked to
package registries only). That means:

1. Variational's endpoint (GET /metadata/stats) IS documented with an
   example response, so that integration should work out of the box.

2. RiseX and Perpl's exact funding-rate endpoints are NOT fully
   documented publicly. The URLs below are best-effort guesses based on
   their docs/API repos. They may be wrong.

3. The `funding_rate_is_percent` and `funding_period_hours` values per
   venue are ASSUMPTIONS. Whether a venue's API returns "0.01" meaning
   0.01% or 0.01 meaning 1%, and whether the rate is per-hour or per-8h,
   changes the APY math a lot and I could not verify it live.

Run this FIRST:

    python3 fetcher.py --test

This hits each venue, prints the raw HTTP status + first ~1500 chars of
the response, and tells you whether the auto-extractor found anything
that looks like funding data. Compare a couple of numbers against what
the venue's own website shows for the same market, then fix the config
at the top of this file (URL, key names, scale, period) in one place.
If a venue's endpoint 404s, check its docs site for the real path and
swap it in — the extractor is written to auto-detect field names, so
you usually only need to fix the URL, not the parsing logic.
────────────────────────────────────────────────────────────────────────
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent  # scripts/ -> repo root
HISTORY_FILE = REPO_ROOT / "data" / "history.jsonl"
OUTPUT_FILE = REPO_ROOT / "funding_data.json"  # sits next to index.html
REQUEST_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (compatible; funding-farm-bot/1.0; +https://example.local)"
HISTORY_RETENTION_DAYS = 10  # keep a little more than 7d for rolling windows

# Field names the auto-extractor looks for on each listing/market object.
TICKER_KEYS = ("ticker", "symbol", "market", "name", "pair")
RATE_KEYS = ("funding_rate", "fundingRate", "funding", "rate", "funding_rate_1h", "funding_rate_8h")

VENUES = {
    "variational": {
        "label": "Variational",
        "url": "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats",
        # Documented endpoint. Example in docs: "funding_rate":"0.037347"
        # Assumption: this is a PERCENT value (0.037347 == 0.037347%), per HOUR.
        # Verify with --test against variational.io's own UI, then fix if wrong.
        "funding_rate_is_percent": True,
        "funding_period_hours": 1,
    },
    "risex": {
        "label": "RiseX",
        # BEST-EFFORT URL — RiseX's public funding-rate endpoint isn't fully
        # documented. api.risex.net/docs/ is their API reference UI; if this
        # 404s, open that page in a browser, find the funding/markets GET
        # endpoint, and paste the real path here.
        "url": "https://api.risex.net/v1/markets",
        "funding_rate_is_percent": True,
        "funding_period_hours": 1,
    },
    "perpl": {
        "label": "Perpl",
        # Documented endpoint for market/chain config — MAY or may not include
        # funding rate. If --test shows no funding field, check
        # github.com/PerplFoundation/api-docs/rest-endpoints.md for the
        # correct funding-specific endpoint and swap it in.
        "url": "https://app.perpl.xyz/api/v1/pub/context",
        "funding_rate_is_percent": True,
        "funding_period_hours": 8,
    },
}

TOP_N_PER_HORIZON = 20
HORIZONS = {"3d": 3, "7d": 7}
MIN_OBS_FOR_CONFIDENCE = 6  # ~1 day of 4h snapshots


# ── HTTP + extraction ───────────────────────────────────────────────────

def fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        status = resp.status
        body = resp.read()
    return status, json.loads(body)


def normalize_symbol(raw: str) -> str:
    s = raw.upper().strip()
    for suf in ("-PERP", "-USD", "-USDT", "_PERP", "_USD", "_USDT", "PERP", "USDT", "USD"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[: -len(suf)]
            s = s.strip("-_")
    return s.strip("-_")


def extract_rates(payload) -> dict:
    """
    Recursively search a parsed JSON payload for any list of dict objects
    that look like a market listing (has both a ticker-like key and a
    funding-rate-like key), and return {normalized_symbol: raw_rate_float}.

    This makes the fetcher resilient to us not knowing the exact response
    shape ahead of time — it works as long as the venue uses one of the
    common field-name conventions in TICKER_KEYS / RATE_KEYS.
    """
    found = {}

    def walk(node):
        if isinstance(node, list):
            for item in node:
                if isinstance(item, dict):
                    tkey = next((k for k in TICKER_KEYS if k in item), None)
                    rkey = next((k for k in RATE_KEYS if k in item), None)
                    if tkey and rkey and item.get(rkey) is not None:
                        try:
                            sym = normalize_symbol(str(item[tkey]))
                            found[sym] = float(item[rkey])
                        except (TypeError, ValueError):
                            pass
                    else:
                        walk(item)
                else:
                    walk(item)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    walk(payload)
    return found


def rate_to_apy_pct(raw_rate: float, venue_cfg: dict) -> float:
    fraction = (raw_rate / 100.0) if venue_cfg["funding_rate_is_percent"] else raw_rate
    periods_per_year = (24.0 / venue_cfg["funding_period_hours"]) * 365.0
    return fraction * periods_per_year * 100.0  # back to percent for display


# ── Fetch one round ─────────────────────────────────────────────────────

def fetch_all(verbose=False):
    """Returns {venue_key: {symbol: apy_pct}}, and prints per-venue status."""
    results = {}
    for key, cfg in VENUES.items():
        try:
            status, payload = fetch_json(cfg["url"])
            rates = extract_rates(payload)
            apy = {sym: rate_to_apy_pct(r, cfg) for sym, r in rates.items()}
            results[key] = apy
            if verbose:
                print(f"[{cfg['label']}] HTTP {status} — {len(apy)} symbols found")
                if not apy:
                    print(f"  ! No funding-like fields detected. Raw response (truncated):")
                    print("   ", json.dumps(payload)[:1500])
        except urllib.error.HTTPError as e:
            print(f"[{cfg['label']}] HTTP ERROR {e.code}: {e.reason}  (url: {cfg['url']})")
            results[key] = {}
        except urllib.error.URLError as e:
            print(f"[{cfg['label']}] CONNECTION ERROR: {e.reason}  (url: {cfg['url']})")
            results[key] = {}
        except Exception as e:
            print(f"[{cfg['label']}] UNEXPECTED ERROR: {type(e).__name__}: {e}")
            results[key] = {}
    return results


# ── History ──────────────────────────────────────────────────────────────

def append_history(run_ts: str, results: dict):
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)  # git doesn't track empty dirs,
    # so data/ won't exist on a fresh checkout until this creates it
    with HISTORY_FILE.open("a") as f:
        for venue, symbols in results.items():
            for sym, apy in symbols.items():
                f.write(json.dumps({"run_ts": run_ts, "venue": venue, "symbol": sym, "apy_pct": apy}) + "\n")


def load_history(max_age_days=HISTORY_RETENTION_DAYS):
    if not HISTORY_FILE.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    rows = []
    with HISTORY_FILE.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                ts = datetime.fromisoformat(row["run_ts"])
                if ts >= cutoff:
                    rows.append(row)
            except Exception:
                continue
    return rows


def prune_history(rows):
    """Rewrite history.jsonl with only the retained rows (keeps file small)."""
    with HISTORY_FILE.open("w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


# ── Scoring ──────────────────────────────────────────────────────────────

def build_pivot(rows):
    """{run_ts: {symbol: {venue: apy_pct}}}"""
    pivot = {}
    for row in rows:
        pivot.setdefault(row["run_ts"], {}).setdefault(row["symbol"], {})[row["venue"]] = row["apy_pct"]
    return pivot


def score_pair(spread_series: list, current_spread: float) -> float:
    """
    spread_series: list of historical spread values (short_apy - long_apy)
    for this symbol+venue-pair within the window, most recent last.

    Score blends magnitude (how big is the current edge) with persistence
    (how often has the edge pointed the same direction historically).
    This is our own heuristic, not any venue's official ranking — tune the
    weights below if you disagree with how it ranks things.
    """
    if not spread_series:
        return 0.0
    magnitude_component = min(abs(current_spread) / 20.0, 1.0) * 70  # cap at 20% APY
    same_sign = sum(1 for s in spread_series if (s >= 0) == (current_spread >= 0))
    persistence = same_sign / len(spread_series)
    persistence_component = persistence * 30
    return round(magnitude_component + persistence_component, 1)


def compute_pairs(pivot: dict, latest_run_ts: str, horizon_days: int):
    cutoff = datetime.now(timezone.utc) - timedelta(days=horizon_days)
    venue_keys = list(VENUES.keys())
    pair_keys = [(venue_keys[i], venue_keys[j]) for i in range(len(venue_keys)) for j in range(i + 1, len(venue_keys))]

    # gather all symbols seen at the latest run
    latest_symbols = pivot.get(latest_run_ts, {})
    out = []

    for symbol, venue_rates in latest_symbols.items():
        for a, b in pair_keys:
            if a not in venue_rates or b not in venue_rates:
                continue
            apy_a, apy_b = venue_rates[a], venue_rates[b]
            if apy_a >= apy_b:
                short_v, long_v, current_spread = a, b, apy_a - apy_b
            else:
                short_v, long_v, current_spread = b, a, apy_b - apy_a

            # historical spread series (short_v - long_v, using each run's
            # own short/long assignment convention frozen to *today's* pairing
            # so the "did the edge persist" check is meaningful)
            series = []
            for run_ts, symbols_at_run in pivot.items():
                try:
                    ts = datetime.fromisoformat(run_ts)
                except Exception:
                    continue
                if ts < cutoff or run_ts == latest_run_ts:
                    continue
                rates_at_run = symbols_at_run.get(symbol)
                if not rates_at_run or short_v not in rates_at_run or long_v not in rates_at_run:
                    continue
                series.append(rates_at_run[short_v] - rates_at_run[long_v])

            n_obs = len(series) + 1  # +1 for the current point
            realized_avg = (sum(series) + current_spread) / n_obs
            score = score_pair(series, current_spread)

            out.append({
                "symbol": symbol,
                "short_venue": short_v,
                "long_venue": long_v,
                "current_spread_apy_pct": round(current_spread, 4),
                "realized_avg_apy_pct": round(realized_avg, 4),
                "short_rate_apy_pct": round(venue_rates[short_v], 4),
                "long_rate_apy_pct": round(venue_rates[long_v], 4),
                "score": score,
                "n_observations": n_obs,
                "confident": n_obs >= MIN_OBS_FOR_CONFIDENCE,
                "hold_days": horizon_days,
            })

    out.sort(key=lambda p: p["score"], reverse=True)
    return out[:TOP_N_PER_HORIZON]


# ── Main ─────────────────────────────────────────────────────────────────

def run(verbose=False):
    run_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    results = fetch_all(verbose=verbose)

    total_symbols = sum(len(v) for v in results.values())
    if total_symbols == 0:
        print("WARNING: no data fetched from any venue this run. Not writing output "
              "(keeping last good funding_data.json so the dashboard doesn't go blank).")
        return

    append_history(run_ts, results)

    rows = load_history()
    prune_history(rows)  # drop anything older than HISTORY_RETENTION_DAYS
    pivot = build_pivot(rows)

    output = {
        "generated_at": run_ts,
        "venues": [{"key": k, "label": v["label"]} for k, v in VENUES.items()],
        "symbols_seen": total_symbols,
        "history_runs": len(pivot),
        "pairs": {},
    }
    for horizon_key, horizon_days in HORIZONS.items():
        output["pairs"][horizon_key] = compute_pairs(pivot, run_ts, horizon_days)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"OK — wrote {OUTPUT_FILE} at {run_ts} "
          f"({total_symbols} symbols, {len(pivot)} history runs retained)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--test", action="store_true",
                         help="Fetch each venue, print raw responses, do NOT write history or output.")
    args = parser.parse_args()

    if args.test:
        fetch_all(verbose=True)
        sys.exit(0)

    run(verbose=False)
