# Funding Farm Dashboard (GitHub + Vercel)

Cross-venue funding-rate spreads for Variational, RiseX, and Perpl. No
server, no VPS, no card on file anywhere. Two free platforms doing what
they're each good at:

- **GitHub Actions** runs `scripts/fetcher.py` every hour on a schedule,
  and commits the result back to this repo.
- **Vercel** hosts `index.html` + `funding_data.json` as a static site, and
  redeploys automatically every time GitHub Actions pushes a commit.

Nothing to SSH into, nothing to renew, nothing that expires.

## Why not just use Vercel's own Cron Jobs?

Because Vercel's free (Hobby) plan caps its own scheduled functions at once
per day — even a 4-hour schedule fails outright, let alone hourly. GitHub
Actions has no such limit on its schedule and is free for this volume of
usage (a few seconds of compute, 24 times a day), so it does the scheduling
and the work, and Vercel just serves the result.

## Repo layout

```
index.html                       the dashboard (served at the site root)
funding_data.json                written by the fetcher, read by the dashboard
scripts/fetcher.py                the fetcher — polls all 3 venues, scores pairs
data/history.jsonl                rolling history (created on first run)
.github/workflows/update-funding.yml   the schedule
.vercelignore                    keeps scripts/.github/data out of the public deploy
```

## Verifying the venue endpoints — do this first

I wrote `fetcher.py` from public API documentation, without the ability to
make live calls to these venues from the environment I built it in. That
means:

- **Variational** — documented endpoint, should work as shipped.
- **RiseX** and **Perpl** — best-effort URLs; I couldn't confirm the exact
  path or field names.

After you've pushed this repo and connected the Actions workflow (see the
deploy guide), go to the **Actions** tab on GitHub, open the
"Update Funding Data" workflow, and click **Run workflow** to trigger it
by hand. Then open that run and expand the **Connectivity check** step —
it prints each venue's HTTP status and either "N symbols found" or the raw
response if nothing was detected.

- **HTTP error / connection failure** → the URL is wrong. Check
  `api.risex.net/docs/` or
  `github.com/PerplFoundation/api-docs/blob/main/rest-endpoints.md` for the
  real endpoint and edit the `url` in the `VENUES` dict near the top of
  `scripts/fetcher.py`.
- **HTTP 200 but "0 symbols found"** → right URL, unrecognized field names.
  Look at the raw response the log printed, and add the actual key names to
  `TICKER_KEYS` / `RATE_KEYS` near the top of the file.
- **Numbers look ~100x off** → check `funding_rate_is_percent` and
  `funding_period_hours` per venue against what the venue's own site shows
  for the same market.

Commit the fix, push, and the next scheduled (or manually triggered) run
picks it up.

## How the scoring works

For every symbol trading on 2+ venues, for every venue pair, the fetcher
shorts whichever side has the higher funding rate and longs the lower one,
then computes:

- **current spread APY** — annualized (short − long) right now
- **realized avg APY** — the average of that spread across the 3D/7D window
- **persistence** — what fraction of historical snapshots had the edge
  pointing the same direction as now
- **score** = `min(|spread| / 20%, 1) × 70 + persistence × 30`

This is my own heuristic (see `score_pair()` in `fetcher.py`), not any
venue's official ranking — the weights are two numbers if you want to tune
them.

**Expect "limited history" on every card for the first several days.** The
7D window needs ~168 runs (7 days × 24/day) before "realized avg" is a real
average rather than a small sample projected forward. The window itself is
still calendar-based (a true 7 days), so this doesn't fill in any faster
than before — you just get much finer-grained data within it.

## Automating trade execution later

Once you trust the numbers: RiseX and Perpl both have real trading APIs, so
opening/closing those legs is automatable from here too — most naturally as
a second GitHub Actions workflow (or a step added to this one) with API
keys stored as GitHub Actions secrets, never committed to the repo.
Variational's trading API isn't public yet, so that leg stays manual until
it is.
