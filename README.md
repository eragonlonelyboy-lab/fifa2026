# Eragon · World Cup 2026 — Predictions vs Reality

A self-updating dashboard that runs **my own** statistical prediction model for the 2026 FIFA
World Cup, fetches **real live results**, and scores **prediction vs reality** — match by match.
One dependency-free Python script regenerates one self-contained `index.html`.

![status](https://img.shields.io/badge/model-RPS_0.171-blue) — beats the open-source baseline (RPS 0.175).

## What it does
- **My model** predicts every match (W/D/L) and runs a Monte-Carlo for title + advancement odds.
- **Live reality**: pulls finished scores from ESPN's keyless JSON (no API key needed).
- **Scorecard**: every finished match shows my pre-match call, the real result, hit/miss, and RPS.
- Computes standings, goals, cards locally — never trusts the provider's tables.
- Smart-writes (only when data changed), atomic writes, backups (last 20), self-checks that abort
  a broken page, full logging.

## The model (the "Eragon WC2026 model")
```
Elo (World-Football) + Dixon-Coles time-decay  ──┐
Squad market value (Transfermarkt, log-scaled)  ──┤→ blended team strength (75% Elo / 25% squad)
Maher attack/defence Poisson (weighted MLE)      ──┘
   → Karlis-Ntzoufras bivariate Poisson (λ₃=0.10) + Dixon-Coles low-score τ (ρ=−0.05)
   → log-opinion-pool ensemble (90% / 10%)
   → Monte Carlo tournament (advancement + champion odds)
   → scored head-to-head vs DraftKings odds (Model vs Market vs Reality)
```
**Squad value** captures current squad quality that results-only Elo lags (e.g. Norway, Ivory
Coast are undervalued by Elo). Adding it moved the live scorecard from 19/36 → **22/36** correct
and RPS 0.161 → **0.159** — and the model now **edges DraftKings on RPS (0.159 vs 0.161)** on the
same 36 games, while never seeing the odds. The squad blend is evidence-based (Groll et al.) and
directionally validated on live games, but not back-tested on historical squad values (those don't
exist in the dataset) — so the headline backtest number stays attributed to the Elo+ensemble core.
- Calibrated on **913 real internationals** (Oct 2023 – Jun 2026), **frozen pre-tournament**
  (honest: no mid-tournament re-fit, so the calls aren't retro-fitted).
- **Walk-forward out-of-sample backtest**: RPS **0.171**, accuracy **62%**, Brier 0.512, log-loss
  0.873, ECE **2.0%** — better than the open-source repo it builds on (RPS 0.175, ECE 2.3%) on
  every metric.
- Method choices are grounded in peer-reviewed work: Maher (1982), Dixon & Coles (1997),
  Karlis & Ntzoufras (2003). The bivariate-Poisson λ₃ term is the verified top-gain addition;
  the attack/defence ensemble is kept at a minority 10% because international data is sparse and
  Elo dominates it there (validated, not assumed).

## Setup
No install. **Python 3.9+** (uses stdlib only: `urllib`, `json`, `zoneinfo`, `hashlib`, …).
```bash
cd dashboard
python scripts/update_dashboard.py --recalibrate    # first run: builds frozen model ratings
python scripts/update_dashboard.py                  # normal run: fetch + render
```
Open `index.html`, or serve it:
```bash
python -m http.server 8770 --directory .            # http://127.0.0.1:8770/index.html
```

## Flags
| Flag | Effect |
|---|---|
| (none) | fetch, compute, smart-write `index.html` if data changed |
| `--force` | rewrite even if data unchanged (for CSS/template tweaks) |
| `--dry-run` | print a JSON summary of what would change; write nothing |
| `--recalibrate` | rebuild frozen model ratings from `data/results.json` |
| `--no-enrich` | skip per-match goal/card enrichment (faster) |
| `--source espn\|apifootball` | choose data provider |

## Data source (swappable)
All network/parse logic is behind `fetch_matches()`. Default = **ESPN** keyless JSON (live,
rich, no key). To use **API-Football** instead, set `APIFOOTBALL_KEY` in your environment and run
`--source apifootball`. Keys are read from env only — **never hardcoded**. Card/scorer stats are
best-effort from the match feed and skip gracefully if a field is missing.

## Timezone
Defaults to the machine's own local zone. Override with `TZ_DISPLAY` (e.g. `TZ_DISPLAY=Asia/Kuala_Lumpur`).

## Schedule it (hourly during the tournament)
**Windows (Task Scheduler):**
```powershell
schtasks /Create /SC HOURLY /TN "WC2026Dashboard" ^
  /TR "python C:\Users\erago\.claude\memory\worldcup2026\dashboard\scripts\update_dashboard.py" ^
  /ST 00:00
```
**macOS / Linux (cron):** `crontab -e`, then:
```
0 * * * * cd /path/to/dashboard && /usr/bin/python3 scripts/update_dashboard.py >> logs/cron.log 2>&1
```

## Layout
```
scripts/update_dashboard.py   the one script that does everything
index.html                    the generated dashboard (overwritten in place)
data/results.json             913 historical internationals (model training)
data/eragon-ratings.json      frozen model ratings (built by --recalibrate)
state/state.json              last-run hash + summary (powers smart-writes)
cache/                        every fetched response, UTC-timestamped (debug parsing)
logs/update.log               one line per run
backups/                      index.html backed up before each overwrite (last 20)
_dev_model.py                 the model's walk-forward validation harness (evidence)
```

Built by Eragon. Model + dashboard are original; the Elo/Dixon-Coles starting point is the
open-source `world-cup-2026-prediction-model` (MIT). No claim to beat the betting market.
