#!/usr/bin/env python3
"""
fetcher.py — polls Variational, RiseX, and Perpl for funding-rate data,
builds a rolling history in data/history.jsonl, computes 3D/7D realized
funding spreads between every pair of venues for every symbol that trades
on 2+ of them, scores each pair, and writes funding_data.json (repo root)
for the dashboard (index.html) to read.

This runs inside a GitHub Actions workflow on a schedule (every 1h) — see
.github/workflows/update-funding.yml and README.md for setup. The workflow
commits the updated funding_data.json / data/history.jsonl back to the
repo, which triggers Vercel to redeploy the static site automatically.
No third-party dependencies: stdlib only (urllib, json).

────────────────────────────────────────────────────────────────────────
Endpoint status (verified live, 2026-08-16 — re-verified against the
venues' own UIs after a user report that Variational's numbers looked
wrong and RiseX never appeared in any pair)
────────────────────────────────────────────────────────────────────────
All three venues are wired to confirmed, verified endpoints and
venue-specific parsers:

1. Variational — GET /metadata/stats. "funding_rate" is ALREADY an
   annualized fraction, not a per-interval percent — confirmed by matching
   it exactly against omni.variational.io/markets' own "Ann. Funding"
   column (HYPE funding_rate="0.1095" == UI's "10.95%", exact). An earlier
   version of this fetcher wrongly annualized it a second time using
   funding_interval_s (1h/4h/8h depending on the market), overstating APY
   by roughly the periods-per-year factor (~11x for 8h-cadence markets).
   Uses a dedicated parser (see "extractor" in VENUES), not the generic
   auto-extractor.

2. RiseX — GET https://api.rise.trade/v1/markets (mainnet). The real API
   host is developer.rise.trade's documented base URL — api.risex.net
   (an earlier guess) doesn't resolve at all. Response is
   {"data": {"markets": [...]}}; funding_rate_8h is a decimal-string
   fraction already normalized to an 8h funding period. base_asset_symbol
   comes back live as the full pair, e.g. "BTC/USDC" — not bare "BTC" like
   the field name implies — so extract_risex_rates() splits on "/" before
   normalizing. Without that split, RiseX's symbols never matched
   Variational's/Perpl's bare tickers and RiseX silently never appeared in
   any pair (no error, just zero overlap). Uses a dedicated parser.

3. Perpl — GET https://app.perpl.xyz/api/v1/pub/context. Schema confirmed
   against github.com/PerplFoundation/api-docs/types.md: the funding rate
   lives at market.funding.rate (a "Micros" value, i.e. raw / 1_000_000 =
   fraction) for a period of market.funding_interval_sec seconds — both
   nested, which is why the generic auto-extractor found 0 symbols here.
   Uses a dedicated parser.

If a venue changes its API shape in the future, run this FIRST:

    python3 fetcher.py --test

This hits each venue, prints the raw HTTP status + first ~1500 chars of
the response, and tells you whether the extractor found anything. Compare
a couple of numbers against what the venue's own site shows for the same
market before trusting the output.
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
        # "funding_rate" is ALREADY an annualized fraction, not a
        # per-interval percent — verified live against
        # omni.variational.io/markets' own "Ann. Funding" column (e.g.
        # HYPE funding_rate="0.1095" == UI's displayed "10.95%" exactly).
        # extract_variational_rates() returns fully annualized APY%
        # directly, bypassing rate_to_apy_pct().
        "extractor": "variational",
    },
    "risex": {
        "label": "RiseX",
        # Confirmed mainnet REST base (developer.rise.trade -> Integration
        # page): https://api.rise.trade. api.risex.net does not resolve.
        "url": "https://api.rise.trade/v1/markets",
        # Response fields are venue-specific (funding_rate_8h already
        # normalized to 8h) — extract_risex_rates() returns fully
        # annualized APY% directly, bypassing rate_to_apy_pct().
        "extractor": "risex",
    },
    "perpl": {
        "label": "Perpl",
        # Confirmed via github.com/PerplFoundation/api-docs/types.md:
        # Context.markets[].funding.rate (Micros) / funding_interval_sec.
        "url": "https://app.perpl.xyz/api/v1/pub/context",
        # extract_perpl_rates() annualizes per-market using each market's
        # own funding_interval_sec and returns APY% directly.
        "extractor": "perpl",
    },
}

TOP_N_PER_HORIZON = 20
HORIZONS = {"3d": 3, "7d": 7}
MIN_OBS_FOR_CONFIDENCE = 24  # ~1 day of 1h snapshots


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


def extract_variational_rates(payload) -> dict:
    """
    Variational-specific parser for GET /metadata/stats.

    IMPORTANT — this contradicts what the docs example implied: "funding_rate"
    is NOT a per-interval rate that needs annualizing via funding_interval_s.
    It is ALREADY an annualized fraction. Verified live on 2026-08-16 against
    omni.variational.io/markets' own "Ann. Funding" column:
        HYPE  funding_rate="0.1095"    -> UI shows "10.95%"  (0.1095 * 100, exact)
        BTC   funding_rate="0.055503"  -> UI showed "5.5%"   (0.055503 * 100)
        ETH   funding_rate="0.047914"  -> UI showed "4.87%"  (0.047914 * 100)
    (BTC/ETH/SOL have small drift vs the UI screenshot since rates update
    continuously and the two reads weren't perfectly simultaneous — HYPE's
    exact match is the strongest signal since it's a thinner, slower-moving
    market.) "funding_interval_s" is payment-cadence metadata only (it's
    1h/4h/8h depending on the market) and must NOT be used in this math —
    an earlier version of this fetcher wrongly treated funding_rate as a
    per-hour percent needing annualization, which overstated APY by roughly
    the periods-per-year factor (~11x for the common 8h-cadence markets).
    """
    if not isinstance(payload, dict):
        return {}
    listings = payload.get("listings")
    if not isinstance(listings, list):
        return {}
    out = {}
    for listing in listings:
        if not isinstance(listing, dict):
            continue
        ticker = listing.get("ticker")
        rate = listing.get("funding_rate")
        if not ticker or rate is None:
            continue
        try:
            apy_pct = float(rate) * 100.0
        except (TypeError, ValueError):
            continue
        out[normalize_symbol(str(ticker))] = apy_pct
    return out


def extract_risex_rates(payload) -> dict:
    """
    RiseX-specific parser for GET /v1/markets (confirmed live via
    developer.rise.trade's "Get market configurations" reference page).

    Response shape: {"data": {"markets": [ {market_id, config: {...}, ...} ]}}.
    The docs group static fields (base_asset_symbol, step sizes) under
    "config", but don't fully clarify whether dynamic fields
    (funding_rate_8h, current_funding_rate, active) live at the top level
    of each market object or nested under "config" too — this checks both
    so it works either way.

    funding_rate_8h is a decimal-string fraction already normalized to an
    8h funding period (e.g. "0.0001" == 0.01% per 8h) — annualize directly.
    Falls back to current_funding_rate * 8 (docs: "funding_rate_8h =
    current_funding_rate × 8") if funding_rate_8h is absent.
    """
    markets = None
    if isinstance(payload, dict):
        data = payload.get("data", payload)
        if isinstance(data, dict):
            markets = data.get("markets")
    if not isinstance(markets, list):
        return {}

    def field(m, key):
        if key in m and m[key] not in (None, ""):
            return m[key]
        cfg = m.get("config") or {}
        if key in cfg and cfg[key] not in (None, ""):
            return cfg[key]
        return None

    out = {}
    for m in markets:
        if not isinstance(m, dict):
            continue
        if field(m, "active") is False:
            continue
        sym_raw = field(m, "base_asset_symbol") or field(m, "display_base_asset_symbol")
        if not sym_raw:
            continue
        # Confirmed live (2026-08-16): base_asset_symbol actually comes back
        # as the full pair, e.g. "BTC/USDC" — not bare "BTC" like the docs'
        # field description implied. normalize_symbol() only strips
        # -USD/-USDT-style suffixes, not a "/QUOTE" separator, so without
        # this split every RiseX symbol failed to match Variational's/
        # Perpl's bare tickers and RiseX never appeared in any pair.
        sym_raw = str(sym_raw).split("/")[0]
        rate_8h = field(m, "funding_rate_8h")
        try:
            if rate_8h is not None:
                rate_8h_val = float(rate_8h)
            else:
                rate_1h = field(m, "current_funding_rate")
                if rate_1h is None:
                    continue
                rate_8h_val = float(rate_1h) * 8.0
        except (TypeError, ValueError):
            continue
        apy_pct = rate_8h_val * (24.0 / 8.0) * 365.0 * 100.0
        out[normalize_symbol(str(sym_raw))] = apy_pct
    return out


def extract_perpl_rates(payload) -> dict:
    """
    Perpl-specific parser for GET /pub/context. Schema confirmed against
    github.com/PerplFoundation/api-docs/types.md:

        Context { markets: Market[] }
        Market  { symbol, name, funding_interval_sec, funding: FundingEvent,
                  config: MarketConfig (has is_open), ... }
        FundingEvent { rate: Micros }   // Micros = raw value * 1e-6 fraction

    rate is the fraction for one funding_interval_sec-length period —
    annualize using each market's own interval rather than a fixed
    venue-wide assumption, since it isn't guaranteed to be the same for
    every market.
    """
    if not isinstance(payload, dict):
        return {}
    markets = payload.get("markets")
    if not isinstance(markets, list):
        return {}

    out = {}
    for m in markets:
        if not isinstance(m, dict):
            continue
        cfg = m.get("config") or {}
        if cfg.get("is_open") is False:
            continue
        sym_raw = m.get("symbol") or m.get("name")
        if not sym_raw:
            continue
        funding = m.get("funding") or {}
        raw_rate = funding.get("rate")
        interval_sec = m.get("funding_interval_sec")
        if raw_rate is None or not interval_sec:
            continue
        try:
            fraction = float(raw_rate) / 1_000_000.0
            interval_hours = float(interval_sec) / 3600.0
            if interval_hours <= 0:
                continue
        except (TypeError, ValueError):
            continue
        periods_per_year = (24.0 / interval_hours) * 365.0
        apy_pct = fraction * periods_per_year * 100.0
        out[normalize_symbol(str(sym_raw))] = apy_pct
    return out


EXTRACTORS = {
    "variational": extract_variational_rates,
    "risex": extract_risex_rates,
    "perpl": extract_perpl_rates,
}


# ── Fetch one round ─────────────────────────────────────────────────────

def fetch_all(verbose=False):
    """Returns {venue_key: {symbol: apy_pct}}, and prints per-venue status."""
    results = {}
    for key, cfg in VENUES.items():
        try:
            status, payload = fetch_json(cfg["url"])
            if "extractor" in cfg:
                apy = EXTRACTORS[cfg["extractor"]](payload)
            else:
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
