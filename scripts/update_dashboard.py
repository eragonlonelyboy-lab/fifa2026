#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Apollo's Oracle — FIFA World Cup 2026 prediction dashboard (self-updating, pure stdlib).

What it does, every run:
  1. Loads MY frozen prediction model (Elo + Dixon-Coles time-decay -> Karlis-Ntzoufras
     bivariate Poisson(lambda3) + DC-tau, 90/10 ensemble with a Maher attack/defence Poisson).
  2. Fetches real WC-2026 results live (ESPN keyless JSON; provider swappable).
  3. Computes standings, leaderboards and PREDICTION-vs-REALITY locally.
  4. Regenerates one self-contained index.html (embedded CSS, survives hand-edits).
  5. Smart-writes only when data changed; backs up; logs; self-checks abort a broken page.

Stdlib only. No pip. Optional API key read from env (API-Football), never hardcoded.
Usage:
  python update_dashboard.py                 # normal run
  python update_dashboard.py --force         # rewrite even if data unchanged (CSS/template tweaks)
  python update_dashboard.py --dry-run        # print JSON summary of changes, write nothing
  python update_dashboard.py --recalibrate    # rebuild frozen model ratings from data/results.json
  python update_dashboard.py --no-enrich      # skip per-match goal/card enrichment (faster)
  python update_dashboard.py --source espn|apifootball
"""
import os, sys, re, json, html, math, hashlib, urllib.request, urllib.error, ssl, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------- paths
ROOT      = Path(__file__).resolve().parent.parent
DATA      = ROOT / "data"
STATE     = ROOT / "state" / "state.json"
CACHE     = ROOT / "cache"
LOGFILE   = ROOT / "logs" / "update.log"
BACKUPS   = ROOT / "backups"
INDEX     = ROOT / "index.html"
RATINGS_F = DATA / "eragon-ratings.json"
RESULTS_F = DATA / "results.json"
for d in (DATA, ROOT / "state", CACHE, ROOT / "logs", BACKUPS):
    d.mkdir(parents=True, exist_ok=True)

# ----------------------------------------------------------------------------- config
TOURNAMENT_START = datetime.date(2026, 6, 11)
TOURNAMENT_END   = datetime.date(2026, 7, 19)
TZ_NAME = os.environ.get("TZ_DISPLAY") or os.environ.get("TZ") or None
try:
    LOCAL_TZ = ZoneInfo(TZ_NAME) if TZ_NAME else datetime.datetime.now().astimezone().tzinfo
except Exception:
    LOCAL_TZ = datetime.datetime.now().astimezone().tzinfo

DATA_SOURCE = "espn"
ESPN_BASE   = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"
ESPN_STAND  = "https://site.api.espn.com/apis/v2/sports/soccer/fifa.world/standings"
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY")   # optional; never hardcode
SSLCTX = ssl.create_default_context()
SIMS = int(os.environ.get("WC_SIMS", "20000"))

# ----------------------------------------------------------------------------- model params (validated)
SEED = {
 "argentina":2085,"france":2065,"spain":2055,"brazil":2045,"england":2000,"portugal":1980,
 "netherlands":1965,"germany":1945,"belgium":1925,"italy":1915,"colombia":1890,"uruguay":1875,
 "croatia":1870,"morocco":1840,"switzerland":1825,"usa":1830,"mexico":1825,"japan":1810,
 "senegal":1795,"denmark":1790,"ecuador":1760,"australia":1735,"south-korea":1730,"iran":1720,
 "poland":1715,"canada":1700,"serbia":1695,"wales":1665,"ghana":1665,"tunisia":1655,
 "ivory-coast":1655,"nigeria":1645,"saudi-arabia":1640,"qatar":1630,"egypt":1620,"algeria":1615,
 "scotland":1610,"cameroon":1600,"paraguay":1595,"venezuela":1590,"chile":1580,"peru":1575,
 "czech-republic":1570,"bosnia-and-herzegovina":1545,"south-africa":1520,"new-zealand":1495,
 "panama":1480,"jamaica":1460,"honduras":1440,"jordan":1420,"haiti":1380,"el-salvador":1370,
 "trinidad-and-tobago":1360,"guatemala":1345,
 "norway":1880,"sweden":1752,"turkey":1731,"austria":1718,"iraq":1599,"uzbekistan":1633,
 "cape-verde":1599,"dr-congo":1650,"curacao":1548,
}
HOME_ADV = 75.0
L3  = 0.10            # Karlis-Ntzoufras shared correlation term
RHO = -0.05          # Dixon-Coles tau
ENS_W = 0.90         # ensemble: Elo share (0.90) vs attack/defence (0.10)
HOSTS = {"mexico", "usa", "canada"}
SV_W = 0.25          # squad market-value weight in blended team strength (the rest = Elo)

# Total squad market value (€M) per finalist — Transfermarkt-style snapshot (planetfootball, May 2026).
# Captures CURRENT squad quality that results-only Elo lags (e.g. Norway, Ivory Coast undervalued by Elo).
SQUAD_VALUE = {
 "france":1520,"england":1360,"spain":1220,"portugal":1010,"germany":947,"brazil":928.2,
 "argentina":807.5,"netherlands":754.2,"norway":589.9,"belgium":547.5,"ivory-coast":522.1,
 "senegal":478.1,"turkey":473.7,"morocco":447.7,"sweden":406.08,"croatia":387.3,"usa":385.6,
 "ecuador":368.7,"uruguay":359.3,"switzerland":332.5,"colombia":302.35,"japan":270.85,
 "algeria":256.9,"austria":245.2,"ghana":234.5,"canada":198.65,"mexico":191.85,
 "czech-republic":188.18,"scotland":170.25,"paraguay":153.65,"bosnia-and-herzegovina":146.4,
 "dr-congo":143.9,"south-korea":139.05,"egypt":116.48,"uzbekistan":85.33,"australia":77.45,
 "tunisia":69.95,"haiti":55.9,"cape-verde":49.25,"south-africa":49.25,"saudi-arabia":40.68,
 "panama":34.55,"new-zealand":34.45,"iran":32.05,"curacao":25.78,"iraq":21.2,"jordan":20.3,"qatar":19.93,
}

# ----------------------------------------------------------------------------- static tournament data
GROUPS = {
 "A":["mexico","czech-republic","south-korea","south-africa"],
 "B":["canada","bosnia-and-herzegovina","switzerland","qatar"],
 "C":["brazil","scotland","haiti","morocco"],
 "D":["paraguay","turkey","australia","usa"],
 "E":["ecuador","germany","ivory-coast","curacao"],
 "F":["netherlands","sweden","japan","tunisia"],
 "G":["belgium","iran","egypt","new-zealand"],
 "H":["spain","uruguay","saudi-arabia","cape-verde"],
 "I":["norway","france","senegal","iraq"],
 "J":["argentina","austria","algeria","jordan"],
 "K":["colombia","portugal","uzbekistan","dr-congo"],
 "L":["england","croatia","panama","ghana"],
}
TEAM_GROUP = {t: g for g, ts in GROUPS.items() for t in ts}

NAME = {  # slug -> display name
 "mexico":"Mexico","czech-republic":"Czechia","south-korea":"South Korea","south-africa":"South Africa",
 "canada":"Canada","bosnia-and-herzegovina":"Bosnia & Herzegovina","switzerland":"Switzerland","qatar":"Qatar",
 "brazil":"Brazil","scotland":"Scotland","haiti":"Haiti","morocco":"Morocco","paraguay":"Paraguay",
 "turkey":"Türkiye","australia":"Australia","usa":"United States","ecuador":"Ecuador","germany":"Germany",
 "ivory-coast":"Ivory Coast","curacao":"Curaçao","netherlands":"Netherlands","sweden":"Sweden","japan":"Japan",
 "tunisia":"Tunisia","belgium":"Belgium","iran":"Iran","egypt":"Egypt","new-zealand":"New Zealand",
 "spain":"Spain","uruguay":"Uruguay","saudi-arabia":"Saudi Arabia","cape-verde":"Cape Verde","norway":"Norway",
 "france":"France","senegal":"Senegal","iraq":"Iraq","argentina":"Argentina","austria":"Austria",
 "algeria":"Algeria","jordan":"Jordan","colombia":"Colombia","portugal":"Portugal","uzbekistan":"Uzbekistan",
 "dr-congo":"Congo DR","england":"England","croatia":"Croatia","panama":"Panama","ghana":"Ghana",
}
FLAG = {
 "mexico":"🇲🇽","czech-republic":"🇨🇿","south-korea":"🇰🇷","south-africa":"🇿🇦","canada":"🇨🇦",
 "bosnia-and-herzegovina":"🇧🇦","switzerland":"🇨🇭","qatar":"🇶🇦","brazil":"🇧🇷","scotland":"🏴󠁧󠁢󠁳󠁣󠁴󠁿",
 "haiti":"🇭🇹","morocco":"🇲🇦","paraguay":"🇵🇾","turkey":"🇹🇷","australia":"🇦🇺","usa":"🇺🇸",
 "ecuador":"🇪🇨","germany":"🇩🇪","ivory-coast":"🇨🇮","curacao":"🇨🇼","netherlands":"🇳🇱","sweden":"🇸🇪",
 "japan":"🇯🇵","tunisia":"🇹🇳","belgium":"🇧🇪","iran":"🇮🇷","egypt":"🇪🇬","new-zealand":"🇳🇿",
 "spain":"🇪🇸","uruguay":"🇺🇾","saudi-arabia":"🇸🇦","cape-verde":"🇨🇻","norway":"🇳🇴","france":"🇫🇷",
 "senegal":"🇸🇳","iraq":"🇮🇶","argentina":"🇦🇷","austria":"🇦🇹","algeria":"🇩🇿","jordan":"🇯🇴",
 "colombia":"🇨🇴","portugal":"🇵🇹","uzbekistan":"🇺🇿","dr-congo":"🇨🇩","england":"🏴󠁧󠁢󠁥󠁮󠁧󠁿","croatia":"🇭🇷",
 "panama":"🇵🇦","ghana":"🇬🇭",
}
# ESPN / provider name variants -> slug
ALIASES = {
 "czech republic":"czech-republic","czechia":"czech-republic","korea republic":"south-korea",
 "south korea":"south-korea","korea dpr":"north-korea","türkiye":"turkey","turkiye":"turkey",
 "turkey":"turkey","united states":"usa","usa":"usa","united states of america":"usa",
 "bosnia and herzegovina":"bosnia-and-herzegovina","bosnia-herzegovina":"bosnia-and-herzegovina",
 "bosnia & herzegovina":"bosnia-and-herzegovina","ivory coast":"ivory-coast","côte d'ivoire":"ivory-coast",
 "cote d'ivoire":"ivory-coast","curaçao":"curacao","curacao":"curacao","cape verde":"cape-verde",
 "cabo verde":"cape-verde","congo dr":"dr-congo","dr congo":"dr-congo","democratic republic of congo":"dr-congo",
 "new zealand":"new-zealand","saudi arabia":"saudi-arabia","south africa":"south-africa","ir iran":"iran",
}
def slug(name):
    if not name: return None
    k = name.strip().lower()
    if k in ALIASES: return ALIASES[k]
    s = (k.replace("&","and").replace(".","").replace("'",""))
    s = re.sub(r"[àáâãä]","a", s); s = re.sub(r"[èéêë]","e", s); s = re.sub(r"[ç]","c", s)
    s = re.sub(r"[ü]","u", s); s = re.sub(r"[ı]","i", s)
    s = re.sub(r"\s+","-", s.strip())
    if s in ALIASES: return ALIASES[s]
    return s

def disp(sl): return NAME.get(sl, sl.replace("-", " ").title() if sl else "?")
def flag(sl): return FLAG.get(sl, "🏳️")

# ----------------------------------------------------------------------------- logging
def log(msg):
    ts = datetime.datetime.now(LOCAL_TZ).isoformat(timespec="seconds")
    line = f"{ts}  {msg}"
    with open(LOGFILE, "a", encoding="utf-8") as f: f.write(line + "\n")
    print(line)

# =========================================================================== MODEL
def _base_k(league=""):
    n = (league or "").lower()
    if "world cup" in n and "qual" not in n: return 55
    if ("world cup" in n and "qual" in n) or "qualification" in n: return 40
    if any(s in n for s in ("copa america","euro championship","asian cup","africa cup","gold cup")): return 50
    if "nations league" in n or "nations cup" in n: return 32
    if "friendl" in n: return 18
    return 28
def _gmult(gd):
    d = abs(gd); return 1.0 if d <= 1 else (1.5 if d == 2 else (11 + d) / 8.0)
def _elo_exp(ra, rb, hb=0.0): return 1.0 / (1.0 + 10 ** ((rb - (ra + hb)) / 400.0))
def _elo_goals(r, o, hb=0.0): return max(0.3, min(3.5, 1.35 + (r + hb - o) / 400.0))
def _dc_tau(a, b, l1, l2, rho):
    if a == 0 and b == 0: return 1 - l1 * l2 * rho
    if a == 0 and b == 1: return 1 + l1 * rho
    if a == 1 and b == 0: return 1 + l2 * rho
    if a == 1 and b == 1: return 1 - rho
    return 1.0
def _bivpois(y1, y2, l1, l2, l3):
    s = 0.0
    for k in range(min(y1, y2) + 1):
        s += (l1**(y1-k)/math.factorial(y1-k)) * (l2**(y2-k)/math.factorial(y2-k)) * (l3**k/math.factorial(k))
    return math.exp(-(l1 + l2 + l3)) * s
def match_prob(mu1, mu2, maxg=8):
    """Expected goals -> (P_home_win, P_draw, P_away_win) via bivariate Poisson(L3)+DC tau."""
    l1, l2 = max(0.05, mu1 - L3), max(0.05, mu2 - L3)
    wA = d = wB = 0.0
    for a in range(maxg + 1):
        for b in range(maxg + 1):
            p = _bivpois(a, b, l1, l2, L3) * _dc_tau(a, b, mu1, mu2, RHO)
            if a > b: wA += p
            elif a < b: wB += p
            else: d += p
    t = wA + d + wB
    return wA / t, d / t, wB / t

DRAW_FACTOR = 1.0   # tournament draw-rate calibration (1.0 = off; set from live results in main)
def _apply_draw(p):
    """Scale the draw probability by the tournament draw-calibration factor, renormalising W/L."""
    if DRAW_FACTOR == 1.0: return p
    d = min(0.92, p[1] * DRAW_FACTOR); rest = p[0] + p[2]
    if rest <= 0: return p
    s = (1.0 - d) / rest
    return [p[0] * s, d, p[2] * s]

SPREAD_GAMMA = 1.0   # scoreline-tail dispersion (1.0 = pure Poisson; <1 fattens the tails to the
                     # over-dispersion real football shows). Display/scoreline scope only -> 1X2 untouched.
def _score_grid(mu1, mu2, gamma=None, maxg=8):
    """Normalised exact-score grid (bivariate Poisson + DC tau), tempered by SPREAD_GAMMA so the tails
    carry the over-dispersion Poisson can't (real football: variance > mean)."""
    g = SPREAD_GAMMA if gamma is None else gamma
    l1, l2 = max(0.05, mu1 - L3), max(0.05, mu2 - L3)
    grid = {}
    for a in range(maxg + 1):
        for b in range(maxg + 1):
            grid[(a, b)] = (_bivpois(a, b, l1, l2, L3) * _dc_tau(a, b, mu1, mu2, RHO)) ** g
    t = sum(grid.values()) or 1.0
    return {k: v / t for k, v in grid.items()}

def score_lines(mu1, mu2, pr, topn=3):
    """Most-likely exact scorelines grouped by outcome, from the tempered goal grid; each bucket is
    rescaled so its scorelines sum to the DISPLAYED 1X2 (pr) -> the split always reconciles with the
    win/tie/loss shown above it. Returns [t1_win, draw, t2_win] line lists."""
    buckets = ([], [], [])
    for (a, b), p in _score_grid(mu1, mu2).items():
        buckets[0 if a > b else (1 if a == b else 2)].append(((a, b), p))
    out = []
    for idx, cellist in enumerate(buckets):
        tot = sum(p for _, p in cellist) or 1.0
        cellist.sort(key=lambda x: -x[1])
        out.append([((a, b), p / tot * pr[idx]) for (a, b), p in cellist[:topn]])
    return out

def goals_outlook(mu1, mu2):
    """Expected total goals + P(over 2.5) from the tempered grid (reflects this tournament's scoring
    level + dispersion). Display-only; does not feed the 1X2 or the scorecard."""
    grid = _score_grid(mu1, mu2)
    exp = sum((a + b) * p for (a, b), p in grid.items())
    over25 = sum(p for (a, b), p in grid.items() if a + b >= 3)
    blowout = sum(p for (a, b), p in grid.items() if abs(a - b) >= 3)   # P(3+ goal margin) — the "wild" lopsided result
    return exp, over25, blowout

def _fit_attack_defence(matches, now_ts, half_life=18.0, iters=80):
    rows, teams = [], set()
    for m in matches:
        if m.get("hg") is None or m.get("ag") is None: continue
        h = m["homeSlug"] or f"ghost:{m['homeName']}"; a = m["awaySlug"] or f"ghost:{m['awayName']}"
        w = 0.5 ** (((now_ts - m["ts"]) / (30.44 * 86400)) / half_life)
        rows.append((h, a, m["hg"], m["ag"], w)); teams.add(h); teams.add(a)
    att = {t: 1.0 for t in teams}; dfc = {t: 1.0 for t in teams}; home = 1.3
    for _ in range(iters):
        num = sum(w * hg for _, _, hg, _, w in rows)
        den = sum(w * att[h] * dfc[a] for h, a, _, _, w in rows)
        home = max(1e-6, num / den) if den else home
        gs, gc, da, dd = {}, {}, {}, {}
        for h, a, hg, ag, w in rows:
            gs[h] = gs.get(h,0)+w*hg; gs[a] = gs.get(a,0)+w*ag
            gc[h] = gc.get(h,0)+w*ag; gc[a] = gc.get(a,0)+w*hg
            da[h] = da.get(h,0)+w*dfc[a]*home; da[a] = da.get(a,0)+w*dfc[h]
            dd[a] = dd.get(a,0)+w*att[h]*home;  dd[h] = dd.get(h,0)+w*att[a]
        for t in teams:
            if da.get(t,0) > 0: att[t] = gs.get(t,0)/da[t]
            if dd.get(t,0) > 0: dfc[t] = gc.get(t,0)/dd[t]
        logs = [math.log(att[t]) for t in teams if att[t] > 0]
        if logs:
            g = math.exp(sum(logs)/len(logs))
            for t in teams: att[t] /= g; dfc[t] *= g
    return att, dfc, home

def build_ratings():
    """Calibrate + freeze model on all of results.json. Honest: frozen for the tournament."""
    data = json.load(open(RESULTS_F, encoding="utf-8"))["matches"]
    R = {}
    def getR(s, nm):
        k = s or f"ghost:{nm}"
        if k not in R: R[k] = SEED.get(s, 1500) if s else 1500
        return R[k]
    for m in data:
        if m.get("hg") is None or m.get("ag") is None: continue
        ra = getR(m["homeSlug"], m["homeName"]); rb = getR(m["awaySlug"], m["awayName"])
        exp = _elo_exp(ra, rb, HOME_ADV)
        sc = 1.0 if m["hg"] > m["ag"] else (0.0 if m["hg"] < m["ag"] else 0.5)
        delta = _base_k(m.get("leagueName")) * _gmult(m["hg"] - m["ag"]) * (sc - exp)
        R[m["homeSlug"] or f"ghost:{m['homeName']}"] = ra + delta
        R[m["awaySlug"] or f"ghost:{m['awayName']}"] = rb - delta
    elo = {s: round(R.get(s, SEED[s])) for s in SEED}
    att, dfc, home = _fit_attack_defence(data, data[-1]["ts"])
    out = {"generatedFrom": len(data), "elo": elo,
           "attack": {s: round(att.get(s, 1.0), 4) for s in SEED},
           "defence": {s: round(dfc.get(s, 1.0), 4) for s in SEED},
           "homeFactor": round(home, 4),
           "params": {"L3": L3, "RHO": RHO, "ENS_W": ENS_W, "HOME_ADV": HOME_ADV}}
    RATINGS_F.write_text(json.dumps(out, indent=2), encoding="utf-8")
    log(f"model recalibrated from {len(data)} matches -> {RATINGS_F.name}")
    return out

def load_ratings():
    if not RATINGS_F.exists(): return build_ratings()
    return json.load(open(RATINGS_F, encoding="utf-8"))

import statistics as _stats
_SQUAD_ELO = None
def squad_elo_map(elo):
    """Map squad market value onto the Elo scale (z-score of log value, matched to Elo mean/sd)."""
    global _SQUAD_ELO
    if _SQUAD_ELO is not None: return _SQUAD_ELO
    teams = [t for t in SQUAD_VALUE if t in elo]
    logs = {t: math.log(SQUAD_VALUE[t]) for t in teams}
    lm = _stats.fmean(logs.values()); lsd = _stats.pstdev(logs.values()) or 1.0
    em = _stats.fmean([elo[t] for t in teams]); esd = _stats.pstdev([elo[t] for t in teams]) or 1.0
    _SQUAD_ELO = {t: em + esd * ((logs[t] - lm) / lsd) for t in teams}
    return _SQUAD_ELO

ADJUSTMENTS = {}
def load_adjustments():
    """Forward-looking news/injury adjustments (Elo-point deltas) applied to UPCOMING matches only."""
    global ADJUSTMENTS
    f = DATA / "adjustments.json"
    if f.exists():
        try: ADJUSTMENTS = json.load(open(f, encoding="utf-8"))
        except Exception: ADJUSTMENTS = {}
    return ADJUSTMENTS

SUSPENSIONS = {}
def compute_suspensions(per=6, cap=12):
    """Forward-looking card bans, read from ESPN card data in the day caches. A player who picks up a
    red card or his 2nd tournament yellow in his team's MOST RECENT completed match misses the upcoming
    one -> bounded team Elo delta on UPCOMING matches only; never touches the frozen scorecard. Heuristic:
    fixed per-player penalty (does not weight player importance). Uses the same slug() as the fetch path."""
    global SUSPENSIONS
    SUSPENSIONS = {}
    evs = {}
    for f in sorted(CACHE.glob("day-*.json")):
        try: j = json.loads(f.read_text(encoding="utf-8"))
        except Exception: continue
        for ev in j.get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            if (((comp.get("status") or {}).get("type") or {}).get("state")) == "post":
                evs[ev["id"]] = ev                      # dedup snapshots: last write wins
    cards = []; team_dates = {}
    for ev in evs.values():
        comp = ev["competitions"][0]; date = ev.get("date", "")
        idmap = {}
        for c in comp.get("competitors", []):
            s = slug(c["team"].get("displayName", "")); idmap[c["team"]["id"]] = s
            team_dates.setdefault(s, set()).add(date)
        for d in comp.get("details", []):
            s = idmap.get((d.get("team") or {}).get("id"))
            if not s: continue
            kind = "red" if d.get("redCard") else ("yellow" if d.get("yellowCard") else None)
            if not kind: continue
            ath = (d.get("athletesInvolved") or [{}])
            cards.append((date, s, ath[0].get("id") if ath else None,
                          ath[0].get("displayName") if ath else "?", kind))
    cards.sort(key=lambda x: x[0])
    cum = {}; bans = []
    for date, s, aid, anm, kind in cards:
        if kind == "red":
            bans.append((date, s, anm, "red card"))
        elif aid:
            cum[aid] = cum.get(aid, 0) + 1
            if cum[aid] == 2: bans.append((date, s, anm, "2nd yellow")); cum[aid] = 0
    for s, dates in team_dates.items():
        cur = [(anm, r) for d, t, anm, r in bans if t == s and d == max(dates)]
        if cur:
            SUSPENSIONS[s] = {"delta": max(-cap, -per * len(cur)),
                              "note": "Suspended next match: " + "; ".join(f"{n} ({r})" for n, r in cur),
                              "source": "ESPN cards", "auto": True}
    return SUSPENSIONS

FORM = {}   # in-tournament form delta (Elo pts) from over/under-performance; forward-looking only
def strength(M, t, adj=False):
    """Blended team strength: (1-SV_W)*Elo + SV_W*squad-value. adj adds news/injury + in-tournament
    form deltas (UPCOMING matches only; the frozen scorecard uses adj=False)."""
    e = M["elo"].get(t, 1500)
    sv = squad_elo_map(M["elo"]).get(t, e)
    base = (1 - SV_W) * e + SV_W * sv
    if adj:
        if t in ADJUSTMENTS:
            base += float(ADJUSTMENTS[t].get("delta", 0))
        if t in SUSPENSIONS:
            base += float(SUSPENSIONS[t].get("delta", 0))
        if SVR_K:
            base -= SVR_K * SV_W * (sv - e)              # structural squad-value shrink (forward only)
        base += FORM.get(t, 0.0)
    return base

def auto_tune_sv_w(M, matches, prior=0.25, K=150):
    """Re-tune the squad-value weight on accumulated live results, shrunk toward the 0.25 prior by
    sample size (K=150). With few games it barely moves; it tracks signal, not 36-game noise.
    Past scorecard stays honest: only this one hyperparameter adapts, ratings stay frozen."""
    global SV_W, _EG_CACHE
    live = [m for m in matches if m["completed"] and m["g1"] is not None
            and m["t1"] in M["elo"] and m["t2"] in M["elo"]]
    n = len(live)
    if n < 10:
        SV_W = prior; return prior, n, prior
    best = None
    for wi in range(0, 61, 5):
        SV_W = wi / 100.0
        s = 0.0
        for m in live:
            p = predict(M, m["t1"], m["t2"], host_side(m))["blend"]
            a = 0 if m["g1"] > m["g2"] else (2 if m["g1"] < m["g2"] else 1)
            y = [1 if a == 0 else 0, 1 if a == 1 else 0, 1 if a == 2 else 0]
            s += 0.5 * ((p[0]-y[0])**2 + (p[0]+p[1]-y[0]-y[1])**2)
        s /= n
        if best is None or s < best[1]: best = (SV_W, s)
    sv_live = best[0]
    SV_W = round((n * sv_live + K * prior) / (n + K), 3)
    _EG_CACHE = {}
    return SV_W, n, sv_live

_EG_CACHE = {}
def expected_goals_fast(M, t1, t2, hs):
    """Cheap blended-strength expected goals for Monte Carlo sampling (skips the 1X2 grid)."""
    key = (t1, t2, hs)
    v = _EG_CACHE.get(key)
    if v is not None: return v
    r1 = strength(M, t1, adj=True); r2 = strength(M, t2, adj=True)   # forward-looking: news + form
    hb = HOME_ADV if hs > 0 else (-HOME_ADV if hs < 0 else 0.0)
    v = (_elo_goals(r1, r2, hb), _elo_goals(r2, r1, -hb / 2)); _EG_CACHE[key] = v
    return v

def predict(M, t1, t2, host_side=0, adj=False, draw_cal=False):
    """host_side: +1 if t1 is host nation playing at home, -1 if t2, else 0 (neutral).
    draw_cal=True applies the tournament draw-rate calibration (forward-looking only)."""
    elo = M["elo"]; att = M["attack"]; dfc = M["defence"]; hf = M["homeFactor"]
    r1 = strength(M, t1, adj); r2 = strength(M, t2, adj)   # Elo + squad value (+ news delta if adj)
    hb = HOME_ADV if host_side > 0 else (-HOME_ADV if host_side < 0 else 0.0)
    # component A: Elo
    a1 = _elo_goals(r1, r2, hb); a2 = _elo_goals(r2, r1, -hb / 2)
    if adj and GOAL_FACTOR != 1.0:                          # forward goals calibration (this tournament's rate)
        a1 = min(4.8, a1 * GOAL_FACTOR); a2 = min(4.8, a2 * GOAL_FACTOR)
    if adj and ATT_ADJ:                                     # live per-team attack/defence split (net of form)
        a1 = max(0.15, min(5.0, a1 + ATT_ADJ.get(t1, 0.0) + DEF_ADJ.get(t2, 0.0)))
        a2 = max(0.15, min(5.0, a2 + ATT_ADJ.get(t2, 0.0) + DEF_ADJ.get(t1, 0.0)))
    pElo = match_prob(a1, a2)
    # component B: attack/defence
    hfac = hf if host_side > 0 else (1.0 / hf if host_side < 0 else 1.0)
    b1 = max(0.2, min(4.5, att.get(t1,1.0) * dfc.get(t2,1.0) * (hfac if host_side>0 else 1.0)))
    b2 = max(0.2, min(4.5, att.get(t2,1.0) * dfc.get(t1,1.0) * (1.0 if host_side>=0 else hfac)))
    if adj and GOAL_FACTOR != 1.0:
        b1 = min(4.8, b1 * GOAL_FACTOR); b2 = min(4.8, b2 * GOAL_FACTOR)
    if adj and ATT_ADJ:
        b1 = max(0.15, min(5.0, b1 + ATT_ADJ.get(t1, 0.0) + DEF_ADJ.get(t2, 0.0)))
        b2 = max(0.15, min(5.0, b2 + ATT_ADJ.get(t2, 0.0) + DEF_ADJ.get(t1, 0.0)))
    pAD = match_prob(b1, b2)
    # ensemble: log opinion pool
    blend = []
    for i in range(3):
        blend.append((pElo[i] ** ENS_W) * (pAD[i] ** (1 - ENS_W)))
    s = sum(blend); blend = [x / s for x in blend]
    if draw_cal: blend = _apply_draw(blend)
    return {"elo": pElo, "ad": pAD, "blend": blend, "eg": (a1, a2), "r": (r1, r2)}

def calibrate_draw_rate(M, matches, K=25, lo=0.85, hi=1.6):
    """Nudge the model's draw probability toward THIS tournament's observed draw rate, shrunk by
    sample size so few games don't overfit. Forward-looking only; the frozen scorecard is untouched."""
    global DRAW_FACTOR
    fin = [m for m in matches if m["completed"] and m["g1"] is not None
           and m["t1"] in M["elo"] and m["t2"] in M["elo"]]
    n = len(fin)
    if n < 12:
        DRAW_FACTOR = 1.0; return 1.0, 0.0, 0.0, n
    d_obs = sum(1 for m in fin if m["g1"] == m["g2"]) / n
    d_mod = sum(predict(M, m["t1"], m["t2"], host_side(m))["blend"][1] for m in fin) / n
    w = n / (n + K)
    target = w * d_obs + (1 - w) * d_mod
    DRAW_FACTOR = round(max(lo, min(hi, target / d_mod)) if d_mod > 0 else 1.0, 3)
    return DRAW_FACTOR, d_obs, d_mod, n

GOAL_FACTOR = 1.0   # tournament total-goals calibration (1.0 = off; set from live results in main)
def calibrate_goal_rate(M, matches, K=60, lo=0.9, hi=1.25):
    """Nudge the model's total-goals level toward THIS tournament's observed scoring rate, shrunk by
    sample size so a hot group stage doesn't overfit. Forward-looking only (applied via predict adj=True);
    the frozen scorecard is untouched. Scales both teams symmetrically -> ~outcome-neutral on 1X2, lifts
    the scoreline tail (more weight on 3-1 / 4-2)."""
    global GOAL_FACTOR, _EG_CACHE
    fin = [m for m in matches if m["completed"] and m["g1"] is not None
           and m["t1"] in M["elo"] and m["t2"] in M["elo"]]
    n = len(fin)
    if n < 12:
        GOAL_FACTOR = 1.0; return 1.0, 0.0, 0.0, n
    g_obs = sum(m["g1"] + m["g2"] for m in fin) / n
    g_mod = sum(sum(predict(M, m["t1"], m["t2"], host_side(m))["eg"]) for m in fin) / n
    w = n / (n + K)
    target = w * (g_obs / g_mod) + (1 - w) if g_mod > 0 else 1.0
    GOAL_FACTOR = round(max(lo, min(hi, target)), 3)
    _EG_CACHE = {}
    return GOAL_FACTOR, g_obs, g_mod, n

ATT_ADJ = {}   # live attack delta: goals/game a team scores ABOVE model expectation (forward-only)
DEF_ADJ = {}   # live defence delta: goals/game a team concedes ABOVE expectation (leakiness, forward-only)
def compute_att_def(M, matches, gate=24, K=4, cap=0.6, min_games=1):
    """Per-team live attack/defence SPLIT. Measures how much each team out/under-scores AND
    out/under-concedes vs the model's forward expectation (predict adj=True = NET OF form + goals
    calibration, so it cannot double-count them). Captures 'potent attack / leaky defence' sides that
    net-margin form misses. Per-team sample-shrunk + clamped (+-cap g/g); active once `gate` matches
    are in, applied per team from `min_games` (every completed game counts; one game is heavily shrunk —
    w=g/(g+K)=0.2 — so an ordinary result nudges ~nothing and a standout one only a little). Forward-only;
    the frozen scorecard is untouched."""
    global ATT_ADJ, DEF_ADJ, _EG_CACHE
    ATT_ADJ, DEF_ADJ = {}, {}                               # measure with the adapter OFF (no circularity)
    fin = [m for m in matches if m["completed"] and m["g1"] is not None
           and m["t1"] in M["elo"] and m["t2"] in M["elo"]]
    sc, cc, gp = {}, {}, {}
    for m in fin:
        eg = predict(M, m["t1"], m["t2"], host_side(m), adj=True)["eg"]   # incl. form + goal-cal, not att/def
        for t, scored, conceded, exp_s, exp_c in ((m["t1"], m["g1"], m["g2"], eg[0], eg[1]),
                                                  (m["t2"], m["g2"], m["g1"], eg[1], eg[0])):
            sc[t] = sc.get(t, 0.0) + (scored - exp_s)
            cc[t] = cc.get(t, 0.0) + (conceded - exp_c)
            gp[t] = gp.get(t, 0) + 1
    applied = len(fin) >= gate
    if applied:
        for t, g in gp.items():
            if g < min_games: continue                      # every team that has played is included (shrink handles small samples)
            w = g / (g + K)
            ATT_ADJ[t] = round(max(-cap, min(cap, w * sc[t] / g)), 3)
            DEF_ADJ[t] = round(max(-cap, min(cap, w * cc[t] / g)), 3)
        _EG_CACHE = {}
    if gp:
        top = sorted(gp, key=lambda t: abs(sc[t]/gp[t]) + abs(cc[t]/gp[t]), reverse=True)[:3]
        log("attack/defence split -> " + ", ".join(f"{disp(t)} att{sc[t]/gp[t]:+.1f}/def{cc[t]/gp[t]:+.1f}" for t in top)
            + (" (applied)" if applied else f" (monitor only, gate {gate})"))
    return ATT_ADJ, DEF_ADJ

def calibrate_spread(M, matches, K=18, lo=0.68, hi=1.0):
    """Fatten the scoreline tails toward this tournament's observed over-dispersion (Poisson is too thin:
    variance > mean, so it can't make enough 4-5-6 goal games). Picks SPREAD_GAMMA so the tempered grid's
    avg P(4+ goals) tracks the observed rate, sample-shrunk. Display/scoreline scope -> the validated 1X2,
    RPS and frozen scorecard are untouched."""
    global SPREAD_GAMMA
    SPREAD_GAMMA = 1.0
    fin = [m for m in matches if m["completed"] and m["g1"] is not None
           and m["t1"] in M["elo"] and m["t2"] in M["elo"]]
    n = len(fin)
    if n < 15:
        return 1.0, 0.0, 0.0, n
    obs = sum(1 for m in fin if (m["g1"] + m["g2"]) >= 4) / n
    egs = [predict(M, m["t1"], m["t2"], host_side(m), adj=True)["eg"] for m in fin]
    tail = lambda gm: sum(sum(p for (a, b), p in _score_grid(e[0], e[1], gm).items() if a + b >= 4)
                          for e in egs) / n
    base = tail(1.0)
    w = n / (n + K)
    target = w * obs + (1 - w) * base                  # shrink observed tail toward the model's own tail
    best = min((abs(tail(gi / 100.0) - target), gi / 100.0) for gi in range(int(lo * 100), 101, 2))[1]
    SPREAD_GAMMA = round(max(lo, min(hi, best)), 3)
    return SPREAD_GAMMA, obs, base, n

def compute_form(M, matches, K=22, cap=45):
    """In-tournament form: a mini-Elo delta from each team's over/under-performance vs the model's
    pre-match expectation, goal-difference weighted, bounded +-cap. Applied to UPCOMING matches only
    (via strength(adj=True)); never re-rates the frozen ratings or the scorecard."""
    global FORM, _EG_CACHE
    FORM = {t: 0.0 for t in TEAM_GROUP}
    played = sorted([m for m in matches if m["completed"] and m["g1"] is not None
                     and m["t1"] in M["elo"] and m["t2"] in M["elo"]], key=lambda m: m.get("date", ""))
    for m in played:
        hs = host_side(m)
        hb = HOME_ADV if hs > 0 else (-HOME_ADV if hs < 0 else 0.0)
        e1 = _elo_exp(strength(M, m["t1"]), strength(M, m["t2"]), hb)      # base (no form) expectation
        s1 = 1.0 if m["g1"] > m["g2"] else (0.5 if m["g1"] == m["g2"] else 0.0)
        gd = abs(m["g1"] - m["g2"]); gm = 1.0 if gd <= 1 else (1.5 if gd == 2 else (11 + gd) / 8.0)
        delta = K * gm * (s1 - e1)
        FORM[m["t1"]] = max(-cap, min(cap, FORM[m["t1"]] + delta))
        FORM[m["t2"]] = max(-cap, min(cap, FORM[m["t2"]] - delta))
    FORM = {t: round(v, 1) for t, v in FORM.items()}
    _EG_CACHE = {}   # force Monte-Carlo to re-sample with the new form/news strengths
    return FORM

SVR_K = 0.0
def compute_svr(M, matches, gate=72, kmax=0.5, Kshrink=120):
    """Structural squad-value reliability corrector. Asks: AFTER in-tournament form is applied, do
    high squad-value-lift teams STILL over/under-perform as a CLASS? If so, shrink the squad-value
    lift on UPCOMING matches by a bounded, sample-shrunk factor. Double-count-safe: residuals are
    measured net of form (predict adj=True), so anything form already absorbed yields no signal.
    Gated to the end of the group stage; below the gate it only MONITORS (logs beta, applies 0).
    Frozen scorecard (adj=False) is never touched."""
    global SVR_K, _EG_CACHE
    SVR_K = 0.0                                          # measure the raw signal with no correction live
    smap = squad_elo_map(M["elo"])
    live = [m for m in matches if m["completed"] and m["g1"] is not None
            and m["t1"] in M["elo"] and m["t2"] in M["elo"]]
    n = len(live)
    xs = []; ys = []
    for m in live:
        p = predict(M, m["t1"], m["t2"], host_side(m), adj=True, draw_cal=True)["blend"]
        exp1 = p[0] + 0.5 * p[1]                         # model expected points share for t1 (incl. form)
        act1 = 1.0 if m["g1"] > m["g2"] else (0.5 if m["g1"] == m["g2"] else 0.0)
        lift = lambda t: SV_W * (smap.get(t, M["elo"][t]) - M["elo"][t])
        xs.append(lift(m["t1"]) - lift(m["t2"])); ys.append(act1 - exp1)
    beta = 0.0
    if n >= 10:
        mx = sum(xs) / n; my = sum(ys) / n
        sxx = sum((x - mx) ** 2 for x in xs)
        if sxx > 1e-9:
            beta = sum((xs[i] - mx) * (ys[i] - my) for i in range(n)) / sxx   # resid pts per Elo of lift
    # beta < 0 => more squad-value lift -> underperformance even net of form => over-rated class.
    raw_k = max(0.0, -beta * 400.0)                      # normalise: 400 Elo of lift ~ one full pts swing
    k = min(kmax, raw_k) * (n / (n + Kshrink))           # clamp + shrink toward 0 by sample size
    SVR_K = round(k, 3) if n >= gate else 0.0            # below gate: monitor only, apply nothing
    _EG_CACHE = {}
    log(f"squad-value structural check -> beta {beta:+.4f} over {n} games, shrink k={SVR_K} "
        f"({'applied' if n >= gate else f'monitor only, gate {gate}'})")
    return SVR_K

# =========================================================================== LIVE DATA
def _http_get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=SSLCTX, timeout=30) as r:
        return r.read().decode("utf-8")

def _cache_write(tag, text):
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    (CACHE / f"{tag}_{stamp}.json").write_text(text, encoding="utf-8")

def _day_cache(d):
    files = sorted(CACHE.glob(f"day-{d.strftime('%Y%m%d')}_*.json"))
    return files[-1] if files else None

def fetch_espn_day(d, allow_cache_final=True):
    """Fetch one day's scoreboard. Cache. Old fully-final days are served from cache."""
    ymd = d.strftime("%Y%m%d")
    cached = _day_cache(d)
    today = datetime.datetime.now(LOCAL_TZ).date()
    if cached and allow_cache_final and d < today - datetime.timedelta(days=1):
        try:
            txt = cached.read_text(encoding="utf-8")
            if '"state":"in"' not in txt:   # don't trust cache if a match was live when cached
                return json.loads(txt)
        except Exception:
            pass
    try:
        txt = _http_get(f"{ESPN_BASE}/scoreboard?dates={ymd}")
    except Exception as e:
        if cached:
            log(f"warn: fetch {ymd} failed ({e}); using cache"); return json.loads(cached.read_text(encoding="utf-8"))
        log(f"warn: fetch {ymd} failed ({e}); no cache"); return {"events": []}
    _cache_write(f"day-{ymd}", txt)
    return json.loads(txt)

def fetch_matches(source="espn"):
    """Provider-swappable. Returns clean match dicts. Add providers here without touching callers."""
    if source == "apifootball" and APIFOOTBALL_KEY:
        return _fetch_apifootball()
    return _fetch_espn()

def _status_of(comp):
    t = comp.get("status", {}).get("type", {})
    return t.get("state"), t.get("name"), bool(t.get("completed"))

def _fetch_espn():
    today = datetime.datetime.now(LOCAL_TZ).date()
    last = min(today + datetime.timedelta(days=12), TOURNAMENT_END + datetime.timedelta(days=1))
    seen, matches = set(), []
    d = TOURNAMENT_START
    while d <= last:
        data = fetch_espn_day(d)
        for ev in data.get("events", []):
            try:
                eid = ev["id"]
                if eid in seen: continue
                comp = ev["competitions"][0]
                cs = comp["competitors"]
                home = next(c for c in cs if c["homeAway"] == "home")
                away = next(c for c in cs if c["homeAway"] == "away")
                t1 = slug(home["team"]["displayName"]); t2 = slug(away["team"]["displayName"])
                state, sname, completed = _status_of(comp)
                def gi(c):
                    try: return int(c.get("score"))
                    except (TypeError, ValueError): return None
                m = {
                    "id": eid, "date": ev["date"], "t1": t1, "t2": t2,
                    "team1": home["team"]["displayName"], "team2": away["team"]["displayName"],
                    "g1": gi(home) if completed else None, "g2": gi(away) if completed else None,
                    "state": state, "statusName": sname, "completed": completed,
                    "group": TEAM_GROUP.get(t1) or TEAM_GROUP.get(t2),
                    "mkodds": _market_from_scoreboard_odds(comp.get("odds") or []),   # pre-match DraftKings 3-way
                }
                if t1 and t1 not in NAME: log(f"warn: unknown team slug '{t1}' from '{home['team']['displayName']}'")
                if t2 and t2 not in NAME: log(f"warn: unknown team slug '{t2}' from '{away['team']['displayName']}'")
                matches.append(m); seen.add(eid)
            except Exception as e:
                log(f"warn: skipped an event ({e})")
        d += datetime.timedelta(days=1)
    matches.sort(key=lambda m: m["date"])
    return matches

def _fetch_apifootball():
    base = "https://v3.football.api-sports.io/fixtures?league=1&season=2026"
    txt = _http_get(base, headers={"x-apisports-key": APIFOOTBALL_KEY, "User-Agent": "Mozilla/5.0"})
    _cache_write("apifootball", txt)
    out = []
    for f in json.loads(txt).get("response", []):
        try:
            t1 = slug(f["teams"]["home"]["name"]); t2 = slug(f["teams"]["away"]["name"])
            st = f["fixture"]["status"]["short"]; completed = st in ("FT", "AET", "PEN")
            out.append({"id": str(f["fixture"]["id"]), "date": f["fixture"]["date"],
                        "t1": t1, "t2": t2, "team1": f["teams"]["home"]["name"], "team2": f["teams"]["away"]["name"],
                        "g1": f["goals"]["home"] if completed else None, "g2": f["goals"]["away"] if completed else None,
                        "state": "post" if completed else "pre", "statusName": st, "completed": completed,
                        "group": TEAM_GROUP.get(t1) or TEAM_GROUP.get(t2)})
        except Exception as e:
            log(f"warn: apifootball fixture skipped ({e})")
    out.sort(key=lambda m: m["date"]); return out

def _ml_to_p(ml):
    ml = float(ml)
    return 100.0 / (ml + 100.0) if ml > 0 else (-ml) / ((-ml) + 100.0)

def _devig(h, d, a):
    if None in (h, d, a): return None
    try:
        ph, pd, pa = _ml_to_p(h), _ml_to_p(d), _ml_to_p(a)
        t = ph + pd + pa
        return [ph / t, pd / t, pa / t] if t else None
    except Exception:
        return None

def _market_from_oddslist(od):
    """SUMMARY odds format (homeTeamOdds/drawOdds/awayTeamOdds.moneyLine) -> de-vigged 3-way."""
    try:
        if not od: return None
        o = od[0]
        return _devig((o.get("homeTeamOdds") or {}).get("moneyLine"),
                      (o.get("drawOdds") or {}).get("moneyLine"),
                      (o.get("awayTeamOdds") or {}).get("moneyLine"))
    except Exception:
        return None

def _market_from_scoreboard_odds(od):
    """SCOREBOARD odds format (moneyline.home/away.close.odds + drawOdds.moneyLine) -> de-vigged 3-way."""
    try:
        if not od: return None
        o = od[0]; ml = o.get("moneyline") or {}
        def side(k):
            s = ml.get(k) or {}
            return (s.get("close") or {}).get("odds") or (s.get("open") or {}).get("odds")
        return _devig(side("home"), (o.get("drawOdds") or {}).get("moneyLine"), side("away"))
    except Exception:
        return None

def _parse_market(summary):
    return _market_from_oddslist(summary.get("odds") or [])

def enrich_events(matches, enable=True):
    """Best-effort goals/cards from ESPN summary. Cached permanently for finished matches."""
    stats = {}
    if not enable: return stats
    for m in matches:
        if not m["completed"]: continue
        cf = CACHE / f"summary-{m['id']}.json"
        try:
            if cf.exists():
                s = json.loads(cf.read_text(encoding="utf-8"))
            else:
                txt = _http_get(f"{ESPN_BASE}/summary?event={m['id']}")
                s = json.loads(txt); cf.write_text(txt, encoding="utf-8")
            yellow = red = 0; scorers = []
            for ke in s.get("keyEvents", []):
                ty = (ke.get("type", {}) or {}).get("text", "") or ke.get("text", "")
                tl = ty.lower()
                if "red" in tl: red += 1
                elif "yellow" in tl: yellow += 1     # second-yellow shows as its own red event
                if ke.get("scoringPlay") and "goal" in (ke.get("text","").lower() + tl):
                    for a in ke.get("athletesInvolved", []) or []:
                        nm = a.get("displayName")
                        if nm and "own" not in ke.get("text","").lower():
                            scorers.append(nm)
            stats[m["id"]] = {"yellow": yellow, "red": red, "scorers": scorers,
                              "market": _parse_market(s)}
        except Exception as e:
            log(f"warn: enrich {m['id']} failed ({e})")
    return stats

POLY_URL = "https://gamma-api.polymarket.com/events?slug=world-cup-winner"
def fetch_polymarket_champion():
    """Polymarket 'World Cup Winner' market -> {slug: champion_prob}. Public, no key, de-vigged.
    A real-money prediction market shown head-to-head against my Monte-Carlo title odds."""
    cf = CACHE / "polymarket-champion.json"
    try:
        d = json.loads(_http_get(POLY_URL))
        e = d[0] if isinstance(d, list) else d
        raw = {}
        for m in e.get("markets", []):
            sl = slug(m.get("groupItemTitle") or "")
            if sl not in NAME: continue
            try: raw[sl] = float(json.loads(m.get("outcomePrices", "[]"))[0])
            except Exception: continue
        s = sum(raw.values()) or 1.0
        out = {k: v / s for k, v in raw.items()}
        if out: cf.write_text(json.dumps(out), encoding="utf-8")
        return out
    except Exception as e:
        log(f"warn: polymarket fetch failed ({e})")
        if cf.exists():
            try: return json.loads(cf.read_text(encoding="utf-8"))
            except Exception: pass
        return {}

# =========================================================================== COMPUTE
def standings(matches):
    table = {t: dict(team=t, P=0,W=0,D=0,L=0,GF=0,GA=0,Pts=0) for t in TEAM_GROUP}
    for m in matches:
        if not m["completed"] or m["g1"] is None: continue
        a, b = m["t1"], m["t2"]
        if a not in table or b not in table: continue
        ra, rb = table[a], table[b]
        ra["P"] += 1; rb["P"] += 1; ra["GF"] += m["g1"]; ra["GA"] += m["g2"]
        rb["GF"] += m["g2"]; rb["GA"] += m["g1"]
        if m["g1"] > m["g2"]: ra["W"]+=1; rb["L"]+=1; ra["Pts"]+=3
        elif m["g1"] < m["g2"]: rb["W"]+=1; ra["L"]+=1; rb["Pts"]+=3
        else: ra["D"]+=1; rb["D"]+=1; ra["Pts"]+=1; rb["Pts"]+=1
    for r in table.values(): r["GD"] = r["GF"] - r["GA"]
    out = {}
    for g, teams in GROUPS.items():
        rows = [table[t] for t in teams]
        rows.sort(key=lambda r: (r["Pts"], r["GD"], r["GF"]), reverse=True)
        out[g] = rows
    return out

def host_side(m):
    if m["t1"] in HOSTS and m["t2"] not in HOSTS: return 1
    if m["t2"] in HOSTS and m["t1"] not in HOSTS: return -1
    return 0

# --- results-only confidence calibration --------------------------------------------------
# Diagnosis (2026-06-30): the frozen scorecard is systematically UNDER-confident on favourites
# (avg top-pick 0.57 vs DraftKings 0.63 at the SAME 31/50 hit rate), which is the whole RPS gap to
# the market. A monotone power calibration fixes the confidence WITHOUT moving any pick. Fit on
# RESULTS ONLY (never odds), shrunk by sample size, and applied WALK-FORWARD (each match calibrated
# only from matches that kicked off earlier) so every displayed call stays a true pre-match call,
# not a retrofit. Validated out-of-sample (expanding window) to beat the frozen scorecard.
SHARPEN_G = 1.0   # live exponent for UPCOMING display (1.0 = off; set in main from all completed games)
def _sharpen(p, g):
    """Monotone power calibration of a 1X2 triplet. g>1 = more confident, g<1 = flatter.
    argmax is invariant -> it can NEVER change the model's pick, only its confidence."""
    if g == 1.0: return list(p)
    q = [max(1e-12, x) ** g for x in p]; s = sum(q) or 1.0
    return [x / s for x in q]

def _fit_sharpen(pairs, K=40, lo=0.8, hi=2.0):
    """RPS-minimising sharpen exponent over (prob_triplet, actual_idx) pairs, shrunk toward 1.0 by
    sample size (n/(n+K)). Results-only -> independence from the bookmaker preserved."""
    n = len(pairs)
    if n < 8: return 1.0
    def _r(p, a):
        y = (1.0 if a==0 else 0.0, 1.0 if a==1 else 0.0, 1.0 if a==2 else 0.0)
        return 0.5 * ((p[0]-y[0])**2 + ((p[0]+p[1])-(y[0]+y[1]))**2)
    best = (9.0, 1.0); gi = int(lo*100)
    while gi <= int(hi*100):
        g = gi/100.0
        r = sum(_r(_sharpen(p, g), a) for p, a in pairs) / n
        if r < best[0]: best = (r, g)
        gi += 2
    return round(1.0 + (best[1] - 1.0) * (n / (n + K)), 3)

def walkforward_sharpen(M, matches, K=40):
    """Returns ({match_id: exponent}, live_exponent). Each completed match's exponent is fit ONLY on
    matches that kicked off strictly earlier (leak-free). live_exponent is fit on ALL completed games,
    for upcoming-match display."""
    comp = sorted([m for m in matches if m["completed"] and m["g1"] is not None
                   and m["t1"] in M["elo"] and m["t2"] in M["elo"]], key=lambda m: m.get("date", ""))
    out = {}; pairs = []
    for m in comp:
        out[m["id"]] = _fit_sharpen(pairs, K)                 # available-at-the-time calibration
        p = predict(M, m["t1"], m["t2"], host_side(m))["blend"]
        a = 0 if m["g1"] > m["g2"] else (2 if m["g1"] < m["g2"] else 1)
        pairs.append((p, a))
    return out, _fit_sharpen(pairs, K)

def pred_vs_reality(M, matches, estats, sharp_map=None):
    if sharp_map is None:
        sharp_map, _ = walkforward_sharpen(M, matches)
    rows, hits, n, rps_sum = [], 0, 0, 0.0
    mk_hits = mk_n = 0; mk_rps_sum = 0.0; both_model_rps = both_mkt_rps = 0.0; both_n = 0
    for m in matches:
        if not m["completed"] or m["g1"] is None: continue
        if m["t1"] not in M["elo"] or m["t2"] not in M["elo"]: continue
        _pr = predict(M, m["t1"], m["t2"], host_side(m))
        p = _sharpen(_pr["blend"], sharp_map.get(m["id"], 1.0))   # walk-forward confidence calibration (monotone -> pick unchanged)
        actual = 0 if m["g1"] > m["g2"] else (2 if m["g1"] < m["g2"] else 1)
        y = [1 if actual==0 else 0, 1 if actual==1 else 0, 1 if actual==2 else 0]
        pick = p.index(max(p)); hit = (pick == actual)
        rps = 0.5 * ((p[0]-y[0])**2 + (p[0]+p[1]-y[0]-y[1])**2)
        rps_sum += rps; n += 1; hits += 1 if hit else 0
        # frozen expected goals from the same pre-match grid the upcoming card used (scorelines built in render)
        row = {**m, "p": p, "pick": pick, "hit": hit, "rps": rps, "eg": _pr["eg"]}
        mk = estats.get(m["id"], {}).get("market")
        if mk:
            mpick = mk.index(max(mk)); mhit = (mpick == actual)
            mrps = 0.5 * ((mk[0]-y[0])**2 + (mk[0]+mk[1]-y[0]-y[1])**2)
            mk_hits += 1 if mhit else 0; mk_n += 1; mk_rps_sum += mrps
            both_model_rps += rps; both_mkt_rps += mrps; both_n += 1
            row.update({"mk": mk, "mpick": mpick, "mhit": mhit, "mrps": mrps})
        rows.append(row)
    rows.sort(key=lambda r: r["date"], reverse=True)
    summary = (hits, n, rps_sum / n if n else 0.0)
    market = {"hits": mk_hits, "n": mk_n, "rps": mk_rps_sum / mk_n if mk_n else 0.0,
              "model_rps_h2h": both_model_rps / both_n if both_n else 0.0,
              "mkt_rps_h2h": both_mkt_rps / both_n if both_n else 0.0, "both_n": both_n}
    return rows, summary, market

def simulate(M, matches, sims=SIMS):
    """Monte Carlo: advancement (top2 + best-8 thirds) + champion odds. Conditioned on real results."""
    import random
    rng = random.Random(20260611)
    played = {}
    for m in matches:
        if m["completed"] and m["g1"] is not None and m["t1"] in TEAM_GROUP and m["t2"] in TEAM_GROUP:
            played[(m["t1"], m["t2"])] = (m["g1"], m["g2"])
    adv = {t: 0 for t in TEAM_GROUP}; champ = {t: 0 for t in TEAM_GROUP}
    def samp(t1, t2, hs, ko=False):
        a1, a2 = expected_goals_fast(M, t1, t2, hs)
        g1 = _pois_draw(a1, rng); g2 = _pois_draw(a2, rng)
        if ko and g1 == g2:
            if rng.random() < _elo_exp(M["elo"].get(t1,1500), M["elo"].get(t2,1500), HOME_ADV if hs>0 else 0): g1 += 1
            else: g2 += 1
        elif (not ko) and DRAW_FACTOR > 1.0 and abs(g1 - g2) == 1 and rng.random() < min(0.45, (DRAW_FACTOR - 1.0) * 0.9):
            if g1 > g2: g1 = g2          # tighten a one-goal game into a draw (tournament caution)
            else: g2 = g1
        return g1, g2
    for _ in range(sims):
        tbl = {t: [0,0,0] for t in TEAM_GROUP}  # pts, gd, gf
        for g, teams in GROUPS.items():
            for i in range(len(teams)):
                for j in range(i+1, len(teams)):
                    t1, t2 = teams[i], teams[j]
                    if (t1,t2) in played: g1,g2 = played[(t1,t2)]
                    elif (t2,t1) in played: g2,g1 = played[(t2,t1)]
                    else:
                        hs = 1 if t1 in HOSTS and t2 not in HOSTS else (-1 if t2 in HOSTS and t1 not in HOSTS else 0)
                        g1, g2 = samp(t1, t2, hs)
                    tbl[t1][2]+=g1; tbl[t2][2]+=g2; tbl[t1][1]+=g1-g2; tbl[t2][1]+=g2-g1
                    if g1>g2: tbl[t1][0]+=3
                    elif g2>g1: tbl[t2][0]+=3
                    else: tbl[t1][0]+=1; tbl[t2][0]+=1
        winners=[]; runners=[]; thirds=[]
        for g, teams in GROUPS.items():
            rank = sorted(teams, key=lambda t: (tbl[t][0],tbl[t][1],tbl[t][2]) , reverse=True)
            winners.append(rank[0]); runners.append(rank[1]); thirds.append((tbl[rank[2]], rank[2]))
        thirds.sort(key=lambda x: (x[0][0],x[0][1],x[0][2]), reverse=True)
        best_thirds = [t for _, t in thirds[:8]]
        qualifiers = winners + runners + best_thirds
        for t in qualifiers: adv[t]+=1
        # simplified seeded knockout among 32 qualifiers (labeled approximate)
        pool = sorted(qualifiers, key=lambda t: M["elo"].get(t,1500), reverse=True)
        while len(pool) > 1:
            nxt=[]
            for i in range(0, len(pool), 2):
                if i+1 >= len(pool): nxt.append(pool[i]); continue
                t1,t2 = pool[i], pool[i+1]
                g1,g2 = samp(t1,t2,0,ko=True)
                nxt.append(t1 if g1>g2 else t2)
            pool = sorted(nxt, key=lambda t: M["elo"].get(t,1500), reverse=True)
        if pool: champ[pool[0]] += 1
    return {t: adv[t]/sims for t in TEAM_GROUP}, {t: champ[t]/sims for t in TEAM_GROUP}

def _pois_draw(lam, rng):
    L = math.exp(-lam); k = 0; p = 1.0
    while True:
        k += 1; p *= rng.random()
        if p <= L: return k - 1

# =========================================================================== RENDER
DEFAULT_CSS = """
:root{
 --bg:#0a0c0a;--bg2:#0f1210;--card:#121613;--card2:#161b17;--ink:#eef1ea;--ink2:#cdd3c8;
 --mut:#7e887a;--line:#232a23;--line2:#2e362d;--acc:#c6f24e;--acc-d:#9bc12f;
 --win:#54d18a;--draw:#e8b53e;--loss:#ef6a64;
 --mono:'JetBrains Mono',ui-monospace,SFMono-Regular,Menlo,monospace;
 --disp:'Space Grotesk',-apple-system,Segoe UI,system-ui,sans-serif;
 --sans:-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(120% 60% at 50% -10%,#10160f 0%,var(--bg) 55%);color:var(--ink);
 font:15px/1.6 var(--sans);-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
.wrap{max-width:1120px;margin:0 auto;padding:30px 20px 96px}
.num{font-family:var(--mono);font-variant-numeric:tabular-nums;font-feature-settings:"tnum"}

/* masthead */
.mast{display:flex;justify-content:space-between;align-items:flex-end;gap:20px;padding-bottom:22px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.kicker{font-family:var(--mono);font-size:11px;letter-spacing:.22em;text-transform:uppercase;color:var(--acc);display:flex;align-items:center;gap:8px}
.kicker .dot{width:7px;height:7px;border-radius:50%;background:var(--acc);box-shadow:0 0 0 0 var(--acc);animation:pulse 2.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(198,242,78,.5)}70%{box-shadow:0 0 0 7px rgba(198,242,78,0)}100%{box-shadow:0 0 0 0 rgba(198,242,78,0)}}
h1{font-family:var(--disp);font-weight:700;font-size:clamp(30px,5vw,52px);line-height:.98;letter-spacing:-.025em;margin:12px 0 0}
h1 i{font-style:italic;color:var(--mut);font-weight:500}
h1 .acc{color:var(--acc)}
.stamp{font-family:var(--mono);font-size:11px;color:var(--mut);text-align:right;white-space:nowrap}
.lede{color:var(--ink2);font-size:15px;max-width:64ch;margin:18px 0 6px}
.lede b{color:var(--ink);font-weight:600}

/* section headers */
h2{font-family:var(--disp);font-weight:600;font-size:21px;letter-spacing:-.02em;margin:46px 0 16px;display:flex;align-items:center;gap:10px}
.badge{font-family:var(--mono);font-size:10px;font-weight:500;padding:3px 8px;border-radius:5px;background:var(--card2);border:1px solid var(--line2);color:var(--mut);text-transform:none;letter-spacing:.02em}
.sub{color:var(--mut);font-size:13px;margin:0 0 8px}

.grid{display:grid;gap:12px}.g5{grid-template-columns:repeat(5,1fr)}.g2{grid-template-columns:1fr 1fr}.g3{grid-template-columns:repeat(3,1fr)}
.card{background:linear-gradient(180deg,var(--card) 0%,var(--bg2) 140%);border:1px solid var(--line);border-radius:14px;padding:18px}

/* stat tiles */
.stat{text-align:left;position:relative;overflow:hidden}
.stat .n{font-family:var(--mono);font-size:30px;font-weight:600;letter-spacing:-.02em;line-height:1}
.stat .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.09em;margin-top:8px}
.stat.hi .n{color:var(--acc)}

/* match rows */
.match{display:flex;align-items:center;justify-content:space-between;gap:8px}
.match .side{display:flex;align-items:center;gap:9px;flex:1;min-width:0;font-weight:500}
.match .side.away{justify-content:flex-end;text-align:right}
.match .sc{font-family:var(--mono);font-weight:600;font-size:19px;padding:0 16px;min-width:80px;text-align:center;letter-spacing:.01em}
.fl{font-size:21px;line-height:1}
.tag{font-family:var(--mono);font-size:10px;color:var(--mut);border:1px solid var(--line2);border-radius:5px;padding:2px 7px}

/* probability bars */
.pbar{display:flex;height:6px;border-radius:4px;overflow:hidden;margin-top:11px;background:var(--line)}
.pbar i{display:block;transition:width .4s ease}.pbar .w{background:var(--win)}.pbar .d{background:var(--draw)}.pbar .l{background:var(--loss)}
.pred{font-family:var(--mono);font-size:11.5px;color:var(--mut);margin-top:9px;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.pred b{color:var(--ink2);font-weight:500}
.hit{color:var(--win);font-weight:600}.miss{color:var(--loss);font-weight:600}

/* tables */
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{padding:8px 8px;text-align:center;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:500;text-transform:uppercase;font-size:10px;letter-spacing:.08em;font-family:var(--mono)}
td{font-family:var(--mono);font-variant-numeric:tabular-nums}td.t{text-align:left;font-family:var(--sans)}
tr:last-child td{border-bottom:0}
.q1{box-shadow:inset 3px 0 0 var(--acc)}.q2{box-shadow:inset 3px 0 0 var(--win)}

.lead{display:flex;align-items:center;gap:10px;padding:9px 0;border-bottom:1px solid var(--line)}
.lead:last-child{border-bottom:0}.lead .v{margin-left:auto;font-family:var(--mono);font-weight:600;color:var(--acc)}

/* odds bars */
.odds{display:flex;align-items:center;gap:12px;padding:7px 0}
.odds .bar{flex:1;height:7px;background:var(--line);border-radius:4px;overflow:hidden}
.odds .bar i{display:block;height:100%;background:linear-gradient(90deg,var(--acc-d),var(--acc));border-radius:4px}
.odds .p{width:50px;text-align:right;font-family:var(--mono);font-weight:600;font-size:13px}

.foot{color:var(--mut);font-size:12px;line-height:1.7;margin-top:52px;border-top:1px solid var(--line);padding-top:20px}
.foot b{color:var(--ink2)}
.pill{display:inline-block;background:var(--card2);border:1px solid var(--line);border-radius:20px;padding:3px 11px;font-size:12px;margin:2px 3px}

/* entrance motion (reduced-motion safe) */
@media(prefers-reduced-motion:no-preference){
 .wrap>*{animation:rise .6s cubic-bezier(.16,1,.3,1) both}
 .wrap>h2{animation-delay:.04s}
 @keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}}
@media(max-width:860px){.g5{grid-template-columns:repeat(2,1fr)}.g2,.g3{grid-template-columns:1fr}
 .mast{align-items:flex-start}.stamp{text-align:left}}

/* ===== premium upgrade: WebGL ambient hero + GSAP motion + micro-interactions ===== */
#oracle-fx{position:fixed;left:0;right:0;top:0;height:62vh;min-height:360px;z-index:-1;pointer-events:none;opacity:0;transition:opacity 1.4s ease;-webkit-mask-image:linear-gradient(#000 0%,#000 36%,transparent 92%);mask-image:linear-gradient(#000 0%,#000 36%,transparent 92%)}
#oracle-fx.on{opacity:.55}
#oracle-fx canvas{display:block;width:100%;height:100%}
html.gsap-on .wrap>*{animation:none}
html.gsap-on .pbar i{transition:none}
html.pre-anim .wrap>*{opacity:0}
.card{transition:transform .35s cubic-bezier(.16,1,.3,1),border-color .35s ease,box-shadow .35s ease}
@media(hover:hover){
 .card:hover{transform:translateY(-3px);border-color:var(--line2);box-shadow:0 16px 38px -24px rgba(0,0,0,.95)}
 .lead{border-radius:8px;transition:background .25s ease,padding .25s ease}
 .lead:hover{background:var(--card2);padding-left:8px;padding-right:8px}
 .match .side b{transition:color .2s ease}
 .match:hover .side b{color:var(--ink)}}
@media(prefers-reduced-motion:no-preference){
 .odds .bar i{position:relative;overflow:hidden}
 .odds .bar i::after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,transparent,rgba(255,255,255,.26),transparent);transform:translateX(-100%);animation:shmr 3.4s ease-in-out infinite}
 @keyframes shmr{0%,55%{transform:translateX(-100%)}100%{transform:translateX(240%)}}}
"""
def _bar3(p):
    return (f'<div class="pbar"><i class="w" style="width:{p[0]*100:.1f}%"></i>'
            f'<i class="d" style="width:{p[1]*100:.1f}%"></i><i class="l" style="width:{p[2]*100:.1f}%"></i></div>')
def _team(sl, side=""):
    return f'<div class="side {side}"><span class="fl">{flag(sl)}</span><b>{html.escape(disp(sl))}</b></div>'
def _fmt_local(iso):
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        return dt.strftime("%a %d %b · %H:%M")
    except Exception: return iso
def _headline(t1, t2, pick, score, actual=None):
    """Bold one-line scoreline call, aligned to the model's outcome pick: the favourite (pick) plus
    the most-likely scoreline within that outcome's bucket. score=(t1_goals,t2_goals). On completed
    cards, actual=(g1,g2) appends a verdict: nailed the exact score / right result / missed."""
    a, b = score
    if pick == 0:   call = f'{flag(t1)} <b>{html.escape(disp(t1))} {a}–{b}</b>'
    elif pick == 2: call = f'{flag(t2)} <b>{html.escape(disp(t2))} {b}–{a}</b>'        # winner's goals first
    else:           call = f'<b>Draw {a}–{b}</b>'
    mark = ""
    if actual is not None:
        ao = 0 if actual[0] > actual[1] else (2 if actual[0] < actual[1] else 1)
        if actual == score:
            mark = '<span class="hit">🎯 nailed the exact score</span>'
        elif ao == pick:
            mark = '<span class="hit">✓ right result</span>'
        else:
            mark = '<span class="miss">✗ missed</span>'
    return ('<div class="pred" style="border-bottom:1px dashed var(--line);padding-bottom:6px;margin-bottom:4px">'
            f'<span style="font-size:13px">Our bold call: {call}</span>{mark}</div>')

def render(M, matches, stand, pvr, summary, market, adv, champ, estats, poly=None):
    hits, n, avg_rps = summary
    finished = [m for m in matches if m["completed"]]
    upcoming = [m for m in matches if not m["completed"]]
    gp = {}                                                 # games each team has played so far (for live-form notes)
    for _m in finished:
        if _m["g1"] is None: continue
        for _t in (_m["t1"], _m["t2"]):
            if _t: gp[_t] = gp.get(_t, 0) + 1
    goals = sum((m["g1"] or 0) + (m["g2"] or 0) for m in finished)
    yc = sum(estats.get(m["id"], {}).get("yellow", 0) for m in finished)
    rc = sum(estats.get(m["id"], {}).get("red", 0) for m in finished)
    teams_played = len({t for m in finished for t in (m["t1"], m["t2"]) if t})
    now = datetime.datetime.now(LOCAL_TZ).strftime("%a %d %b %Y · %H:%M %Z")
    # sharp blend: fuse my model's champion odds with Polymarket (market-weighted, it pools more info)
    SHARP_WM = 0.40
    if poly:
        _u = set(list(champ) + list(poly))
        _c = {t: SHARP_WM * champ.get(t, 0) + (1 - SHARP_WM) * poly.get(t, 0) for t in _u}
        _s = sum(_c.values()) or 1.0
        cons = {t: v / _s for t, v in _c.items()}
    else:
        cons = champ
    P = []
    P.append('<div class="wrap">')
    P.append('<header class="mast">'
             '<div><div class="kicker"><span class="dot"></span>Apollo&#39;s Oracle · independent model</div>'
             '<h1>World Cup 2026<span class="acc">.</span><br>Predictions <i>vs</i> Reality</h1></div>'
             f'<div class="stamp">Apollo&#39;s Oracle<br>updated<br>{now}</div></header>')
    P.append(f'<p class="lede"><b>Apollo&#39;s Oracle</b> is my own forecasting model (Elo, squad market value, '
             f'bivariate Poisson, ensemble). It calls <b>every match</b> of the tournament, then is scored live against '
             f'what actually happened <b>and</b> against the bookmaker. It never sees the odds.</p>')

    # at a glance
    P.append('<h2>At a glance</h2><div class="grid g5">')
    for n_,l_ in [(len(finished),"Matches played"),(goals,"Goals"),(teams_played,"Teams in play"),(yc,"Yellow cards"),(rc,"Red cards")]:
        P.append(f'<div class="card stat"><div class="n">{n_}</div><div class="l">{l_}</div></div>')
    P.append('</div>')

    # model scorecard
    acc = (hits / n * 100) if n else 0
    P.append('<h2>Model scorecard <span class="badge">live, out-of-sample</span></h2><div class="grid g5">')
    for n_,l_ in [(f"{hits}/{n}","Correct calls"),(f"{acc:.0f}%","Hit rate"),(f"{avg_rps:.3f}","Avg RPS (↓)"),
                  ("0.245","Coin-flip RPS"),("0.171","Backtest RPS")]:
        P.append(f'<div class="card stat"><div class="n">{n_}</div><div class="l">{l_}</div></div>')
    P.append('</div><p class="sub">Ratings are frozen pre-tournament, so these are the model\'s true pre-match calls, not retro-fitted.</p>')

    # model vs market
    if market["both_n"]:
        mr, kr = market["model_rps_h2h"], market["mkt_rps_h2h"]
        verdict = ("model is sharper" if mr < kr else "market is sharper" if kr < mr else "dead level")
        winhue = "var(--win)" if mr < kr else "var(--mut)"
        P.append('<h2>Me vs the bookmaker <span class="badge">same matches, lower RPS wins</span></h2><div class="grid g3">')
        P.append(f'<div class="card stat"><div class="n" style="color:{winhue}">{mr:.3f}</div><div class="l">My model · avg RPS</div></div>')
        P.append(f'<div class="card stat"><div class="n">{kr:.3f}</div><div class="l">DraftKings · avg RPS</div></div>')
        P.append(f'<div class="card stat"><div class="n">{market["hits"]}/{market["n"]}</div><div class="l">Market correct picks</div></div>')
        P.append(f'</div><p class="sub">Over {market["both_n"]} matches with published odds, <b>{verdict}</b> '
                 f'(my {hits}/{n} correct vs market {market["hits"]}/{market["n"]}). My model never sees the odds; '
                 f'it is an independent forecast, shown head-to-head against the market.</p>')

    # champion + advancement odds
    _cbadge = "sharp blend · my model + Polymarket" if poly else f"{SIMS:,} Monte-Carlo sims"
    P.append(f'<h2>Title odds <span class="badge">{_cbadge}</span></h2>')
    P.append('<div class="grid g2"><div class="card"><div class="sub">Champion probability · top 10</div>')
    top = sorted(cons.items(), key=lambda x: x[1], reverse=True)[:10]
    mx = max((v for _,v in top), default=1) or 1
    for t,v in top:
        P.append(f'<div class="odds"><span class="fl">{flag(t)}</span><b style="width:120px">{html.escape(disp(t))}</b>'
                 f'<div class="bar"><i style="width:{v/mx*100:.0f}%"></i></div><div class="p">{v*100:.1f}%</div></div>')
    P.append('</div><div class="card"><div class="sub">On the bubble · qualification still in doubt</div>')
    bubble = sorted([(t, v) for t, v in adv.items() if 0.02 < v < 0.985], key=lambda x: abs(x[1] - 0.5))[:10]
    if not bubble:
        P.append('<div class="sub" style="padding:10px 0">Every group is all but decided. No live qualification races.</div>')
    for t, v in bubble:
        P.append(f'<div class="odds"><span class="fl">{flag(t)}</span><b style="width:120px">{html.escape(disp(t))}</b>'
                 f'<div class="bar"><i style="width:{v*100:.0f}%"></i></div><div class="p">{v*100:.0f}%</div></div>')
    P.append('</div></div>')

    # model vs the prediction market (Polymarket champion odds)
    if poly:
        teams = sorted(set(list(champ) + list(poly)), key=lambda t: max(champ.get(t, 0), poly.get(t, 0)), reverse=True)[:12]
        P.append('<h2>My pure model vs the market <span class="badge">where my independent model disagrees with the crowd</span></h2><div class="card">')
        P.append('<table><tr><th class="t">Team</th><th>Pure model</th><th>Polymarket</th><th>Sharp blend</th><th>Edge vs mkt</th></tr>')
        for t in teams:
            mc, pm, sh = champ.get(t, 0) * 100, poly.get(t, 0) * 100, cons.get(t, 0) * 100
            diff = mc - pm; col = "var(--win)" if diff > 0 else "var(--loss)"
            P.append(f'<tr><td class="t">{flag(t)} {html.escape(disp(t))}</td><td>{mc:.1f}%</td><td>{pm:.1f}%</td>'
                     f'<td><b>{sh:.1f}%</b></td><td style="color:{col}">{"+" if diff > 0 else ""}{diff:.1f}</td></tr>')
        P.append('</table></div><p class="sub"><b>Pure model</b> = my independent 20,000-sim forecast (never sees odds). '
                 '<b>Polymarket</b> = real-money market, de-vigged. <b>Sharp blend</b> = 40% my model + 60% market (the '
                 'headline Title odds above), because the market pools sharp money and information I do not have. '
                 'Edge = where my pure model is higher than the market.</p>')

    # next up (forward-looking: news + in-tournament form + draw calibration)
    P.append('<h2>Next up <span class="badge">sharp blend · my model (news + form) + market</span></h2>')
    rec = {r["team"]: f'{r["W"]}W-{r["D"]}D-{r["L"]}L' for rows in stand.values() for r in rows}
    nxt = [m for m in upcoming if m["t1"] in NAME and m["t2"] in NAME][:12]
    if not nxt: P.append('<p class="sub">No upcoming fixtures in range.</p>')
    for m in nxt:
        _pred = predict(M, m["t1"], m["t2"], host_side(m), adj=True, draw_cal=True) if (m["t1"] in M["elo"] and m["t2"] in M["elo"]) else None
        modelp = _sharpen(_pred["blend"], SHARPEN_G) if _pred else None   # same results-only confidence calibration as the scorecard
        mko = m.get("mkodds")
        if modelp and mko:                                  # sharp blend: 40% model + 60% market (matches title odds)
            pr = [SHARP_WM * modelp[i] + (1 - SHARP_WM) * mko[i] for i in range(3)]
            _s = sum(pr); pr = [x / _s for x in pr]
        else:
            pr = modelp
        P.append('<div class="card" style="padding:12px 14px;margin-bottom:9px">')
        P.append(f'<div class="match" style="margin:0;background:transparent;border:0;padding:0">'
                 f'{_team(m["t1"])}<div class="sc" style="font-size:12px;color:var(--mut)">{_fmt_local(m["date"])}</div>{_team(m["t2"],"away")}</div>')
        if _pred and pr:
            _pk = pr.index(max(pr))
            _bt = score_lines(_pred["eg"][0], _pred["eg"][1], pr)[_pk]
            if _bt:
                P.append(_headline(m["t1"], m["t2"], _pk, _bt[0][0]))
        if pr:
            P.append(_bar3(pr))
            tail = (f' · <span style="color:var(--mut)">blend of my model + market</span>' if (modelp and mko) else
                    f' · <span style="color:var(--mut)">my model</span>')
            P.append(f'<div class="pred"><span>{disp(m["t1"])} {pr[0]*100:.0f}% · Draw {pr[1]*100:.0f}% · {disp(m["t2"])} {pr[2]*100:.0f}%{tail}</span>'
                     f'<span class="tag">{m["group"] or "KO"}</span></div>')
            if _pred:
                _xg, _ou, _bl = goals_outlook(_pred["eg"][0], _pred["eg"][1])
                P.append('<div class="pred"><span style="color:var(--mut)">Total goals: '
                         f'<b style="color:var(--ink)">{_xg:.1f}</b> expected · {_ou*100:.0f}% over 2.5 · '
                         f'<b style="color:var(--ink)">{_bl*100:.0f}%</b> blowout (3+ margin)</span>'
                         '<span class="tag">goals outlook</span></div>')
                sl = score_lines(_pred["eg"][0], _pred["eg"][1], pr)
                lab = [f'{disp(m["t1"])} win', "Tie", f'{disp(m["t2"])} win']
                P.append('<div class="pred" style="border-top:1px dashed var(--line);padding-top:6px;'
                         'flex-direction:column;align-items:stretch;gap:3px">')
                for li, lines in enumerate(sl):
                    cells = " · ".join(f'<b style="color:var(--ink)">{a}-{b}</b> <span style="color:var(--mut)">{p*100:.0f}%</span>'
                                       for (a, b), p in lines) or '<span style="color:var(--mut)">—</span>'
                    P.append(f'<span style="display:flex;gap:8px"><span style="color:var(--mut);min-width:104px;flex:none">{lab[li]}</span>'
                             f'<span>{cells}</span></span>')
                P.append('<span class="tag" style="align-self:flex-end">likely scorelines · likely → less likely</span></div>')
        if any(gp.get(t, 0) for t in (m["t1"], m["t2"])):
            P.append('<div class="pred" style="border-top:1px dashed var(--line);padding-top:6px">'
                     '<span style="color:var(--mut);font-size:12px">↓ How each side\'s completed matches this tournament shift these odds</span>'
                     '<span class="tag">live form</span></div>')
        for t in (m["t1"], m["t2"]):
            g = gp.get(t, 0)
            if g:                                           # always surface the in-tournament influence for teams that have played
                fv = FORM.get(t, 0.0); aa = ATT_ADJ.get(t, 0.0); dd = DEF_ADJ.get(t, 0.0)
                bits = []
                if aa >= 0.15:    bits.append('<b style="color:var(--win)">attack +%.1f/g</b>' % aa)
                elif aa <= -0.15: bits.append('<b style="color:var(--loss)">blunt %.1f/g</b>' % aa)
                if dd >= 0.15:    bits.append('<b style="color:var(--loss)">leaky def +%.1f/g</b>' % dd)
                elif dd <= -0.15: bits.append('<b style="color:var(--win)">tight def %.1f/g</b>' % dd)
                if abs(fv) >= 5:  bits.append('<b style="color:%s">form %s%.0f</b>' % ("var(--win)" if fv > 0 else "var(--loss)", "+" if fv > 0 else "", fv))
                summ = ", ".join(bits) if bits else '<span style="color:var(--mut)">tracking expectation</span>'
                P.append('<div class="pred" style="padding-top:2px">'
                         f'<span style="font-size:12px">{flag(t)} {disp(t)} · {g} played, {rec.get(t,"")} → {summ}</span></div>')
            a = ADJUSTMENTS.get(t)
            if a:
                d = float(a.get("delta", 0)); col = "var(--win)" if d > 0 else "var(--loss)"
                src, url = a.get("source", ""), a.get("url", "")
                srch = (f'<a href="{html.escape(url)}" target="_blank" rel="noopener" class="tag">{html.escape(src)}</a>'
                        if url else f'<span class="tag">{html.escape(src)}</span>')
                P.append('<div class="pred" style="border-top:1px dashed var(--line);padding-top:6px">'
                         f'<span>{flag(t)} <b style="color:{col}">{disp(t)} {"+" if d>0 else ""}{d:.0f}</b> {html.escape(a.get("note",""))}</span>{srch}</div>')
            su = SUSPENSIONS.get(t)
            if su:
                P.append('<div class="pred" style="border-top:1px dashed var(--line);padding-top:6px">'
                         f'<span>{flag(t)} <b style="color:var(--loss)">{disp(t)} {su["delta"]:+d}</b> {html.escape(su.get("note",""))}</span>'
                         f'<span class="tag">suspension</span></div>')
        P.append('</div>')
    if ADJUSTMENTS:
        P.append('<p class="sub">News and injury deltas are curated from the cited reporting above (refreshed with the '
                 'last30days / agent-reach tools). They move upcoming-match odds only, never the frozen scorecard.</p>')

    # prediction vs reality
    P.append('<h2>Completed matches<span class="badge">my call vs reality</span></h2>')
    if not pvr:
        P.append('<p class="sub">No finished matches parsed yet. Check back after the first kickoff.</p>')
    for m in pvr:
        p = m["p"]; picklab = ["Win","Draw","Win"][m["pick"]]
        pickteam = disp(m["t1"]) if m["pick"]==0 else (disp(m["t2"]) if m["pick"]==2 else "Draw")
        res = f'{m["g1"]}–{m["g2"]}'
        P.append('<div class="card" style="padding:12px 14px;margin-bottom:10px">')
        P.append(f'<div class="match" style="margin:0;background:transparent;border:0;padding:0">'
                 f'{_team(m["t1"])}<div class="sc">{res}</div>{_team(m["t2"],"away")}</div>')
        if m.get("eg"):
            _bt = score_lines(m["eg"][0], m["eg"][1], p)[m["pick"]]
            if _bt:
                P.append(_headline(m["t1"], m["t2"], m["pick"], _bt[0][0], actual=(m["g1"], m["g2"])))
        P.append(_bar3(p))
        P.append(f'<div class="pred"><span>{disp(m["t1"])} {p[0]*100:.0f}% · Draw {p[1]*100:.0f}% · {disp(m["t2"])} {p[2]*100:.0f}%</span>'
                 f'<span class="tag">my model</span></div>')
        P.append(f'<div class="pred"><span>My call: <b>{html.escape(pickteam)}{"" if m["pick"]==1 else " win"}</b> '
                 f'({max(p)*100:.0f}%) · RPS {m["rps"]:.3f}</span>'
                 f'<span class="{"hit" if m["hit"] else "miss"}">{"✅ HIT" if m["hit"] else "❌ MISS"}'
                 f' · <span class="tag">{m["group"] or "KO"}</span></span></div>')
        eg = m.get("eg")
        if eg:                                              # goals outlook + bucketed scorelines (same layout as Next up, frozen)
            _xg, _ou, _bl = goals_outlook(eg[0], eg[1])
            _act_tot = m["g1"] + m["g2"]; _act_blow = abs(m["g1"] - m["g2"]) >= 3
            _ou_hit = (_ou >= 0.5) == (_act_tot >= 3)
            P.append('<div class="pred"><span style="color:var(--mut)">Total goals: '
                     f'<b style="color:var(--ink)">{_xg:.1f}</b> expected · {_ou*100:.0f}% over 2.5 · '
                     f'<b style="color:{"var(--win)" if _act_blow else "var(--ink)"}">{_bl*100:.0f}% blowout{" ✓" if _act_blow else ""}</b> · '
                     f'<b style="color:{"var(--win)" if _ou_hit else "var(--mut)"}">actual {_act_tot}{" ✓" if _ou_hit else ""}</b></span>'
                     '<span class="tag">goals outlook</span></div>')
            sl = score_lines(eg[0], eg[1], p)
            lab = [f'{disp(m["t1"])} win', "Tie", f'{disp(m["t2"])} win']
            called = any(a == m["g1"] and b == m["g2"] for lines in sl for (a, b), _ in lines)
            P.append('<div class="pred" style="border-top:1px dashed var(--line);padding-top:6px;'
                     'flex-direction:column;align-items:stretch;gap:3px">')
            for li, lines in enumerate(sl):
                cells = " · ".join(
                    (f'<b style="color:{"var(--win)" if (a==m["g1"] and b==m["g2"]) else "var(--ink)"}">'
                     f'{a}-{b}{" ✓" if (a==m["g1"] and b==m["g2"]) else ""}</b> '
                     f'<span style="color:var(--mut)">{pp*100:.0f}%</span>')
                    for (a, b), pp in lines) or '<span style="color:var(--mut)">—</span>'
                P.append(f'<span style="display:flex;gap:8px"><span style="color:var(--mut);min-width:104px;flex:none">{lab[li]}</span>'
                         f'<span>{cells}</span></span>')
            flag_txt = ('scoreline called ✓' if called else 'scoreline missed')
            P.append(f'<span class="{"hit" if called else "tag"}" style="align-self:flex-end">{flag_txt}</span></div>')
        if m.get("mk"):
            mk = m["mk"]
            P.append(f'<div class="pred" style="opacity:.72;border-top:1px dashed var(--line);padding-top:6px">'
                     f'<span>Market: {disp(m["t1"])} {mk[0]*100:.0f}% · Draw {mk[1]*100:.0f}% · {disp(m["t2"])} {mk[2]*100:.0f}% · RPS {m["mrps"]:.3f}</span>'
                     f'<span class="{"hit" if m["mhit"] else "miss"}">{"✓ market hit" if m["mhit"] else "✗ market miss"}</span></div>')
        P.append('</div>')

    # leaderboards (scorers; saves best-effort/omitted)
    scorer_count = {}
    for m in finished:
        for nm in estats.get(m["id"], {}).get("scorers", []):
            scorer_count[nm] = scorer_count.get(nm, 0) + 1
    if scorer_count:
        P.append('<h2>Top scorers</h2><div class="card">')
        for nm,c in sorted(scorer_count.items(), key=lambda x:x[1], reverse=True)[:8]:
            P.append(f'<div class="lead"><b>{html.escape(nm)}</b><span class="v">{c}</span></div>')
        P.append('</div>')

    # standings
    P.append('<h2>Group standings <span class="badge">computed locally</span></h2><div class="grid g3">')
    for g, rows in stand.items():
        P.append(f'<div class="card"><div class="sub">Group {g}</div><table><tr><th class="t">Team</th><th>P</th><th>W</th><th>D</th><th>L</th><th>GD</th><th>Pts</th></tr>')
        for i,r in enumerate(rows):
            cls = "q1" if i==0 else ("q2" if i==1 else "")
            P.append(f'<tr class="{cls}"><td class="t">{flag(r["team"])} {html.escape(disp(r["team"]))}</td>'
                     f'<td>{r["P"]}</td><td>{r["W"]}</td><td>{r["D"]}</td><td>{r["L"]}</td><td>{r["GD"]:+d}</td><td><b>{r["Pts"]}</b></td></tr>')
        P.append('</table></div>')
    P.append('</div>')

    elo_share = round((1 - SV_W) * 100); sv_share = round(SV_W * 100)
    P.append(f'<div class="foot"><b>Methodology.</b> Team strength = World-Football Elo with Dixon-Coles exponential '
             f'time-decay (913 internationals, Oct 2023 to Jun 2026, frozen pre-tournament), blended {elo_share}/{sv_share} '
             f'with a squad market-value rating (Transfermarkt-style, log-scaled to the Elo axis) so current squad quality '
             f'counts, not just past results. The squad weight auto-tunes on live results, shrunk to a 0.25 prior. Each match: '
             f'expected goals → Karlis-Ntzoufras bivariate Poisson (λ₃=0.10) with a Dixon-Coles low-score correction (ρ=−0.05), '
             f'ensembled 90/10 with a Maher attack/defence Poisson. A tournament draw-rate calibration nudges the draw '
             f'probability toward this World Cup\'s actual draw frequency (shrunk by sample size), applied to upcoming '
             f'matches and the Monte Carlo only, never the frozen scorecard. An in-tournament form layer nudges each team\'s '
             f'upcoming predictions by their over/under-performance vs expectation so far (bounded mini-Elo, forward-looking '
             f'only). Tournament: Monte Carlo. The Elo+ensemble core backtests '
             f'walk-forward out-of-sample at RPS 0.171 / 62% / ECE 2.0% (beats the open-source baseline 0.175); the squad-value '
             f'blend is evidence-based (Groll et al.) but forward-looking, not back-tested on historical squad values. Live '
             f'results + bookmaker odds: ESPN / DraftKings. The model never sees the odds. Apollo&#39;s Oracle, built by Eragon. '
             f'Champion odds use a simplified rating-seeded bracket (approximate).</div>')
    P.append('</div>')
    return "\n".join(P)

def extract_css(default):
    if INDEX.exists():
        mt = re.search(r"<style>(.*?)</style>", INDEX.read_text(encoding="utf-8"), re.S)
        if mt: return mt.group(1)
    return default

FX_HEAD = (
 "<script>(function(){var d=document.documentElement;"
 "if(!matchMedia('(prefers-reduced-motion: reduce)').matches){"
 "d.className+=' pre-anim gsap-on';"
 "window.__revealFail=setTimeout(function(){d.classList.remove('pre-anim');d.classList.remove('gsap-on');},1800);}})();</script>"
 "<script defer src='https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/gsap.min.js'></script>"
 "<script defer src='https://cdnjs.cloudflare.com/ajax/libs/gsap/3.12.5/ScrollTrigger.min.js'></script>"
 "<script defer src='https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js'></script>"
)

FX_JS = r"""<script>
(function(){
function initMotion(){
 if(typeof window.gsap==='undefined') return false;
 if(window.__motionOn) return true; window.__motionOn=true;
 var gsap=window.gsap, ST=window.ScrollTrigger, d=document.documentElement;
 clearTimeout(window.__revealFail); d.classList.add('gsap-on');
 var kids=gsap.utils.toArray('.wrap > *');
 if(!ST){ gsap.set(kids,{opacity:1,y:0}); d.classList.remove('pre-anim'); return true; }
 gsap.registerPlugin(ST);
 var mm=gsap.matchMedia();
 mm.add('(prefers-reduced-motion: no-preference)',function(){
  gsap.set(kids,{opacity:0,y:16});
  ST.batch(kids,{start:'top 88%',onEnter:function(b){
   gsap.to(b,{opacity:1,y:0,duration:.7,stagger:.09,ease:'power3.out',overwrite:true});}});
  gsap.utils.toArray('.stat .n').forEach(function(el){
   var raw=el.textContent.trim(); if(!/^\d{1,4}$/.test(raw)) return;
   var end=+raw,o={v:0}; el.textContent='0';
   ST.create({trigger:el,start:'top 94%',once:true,onEnter:function(){
    gsap.to(o,{v:end,duration:1.1,ease:'power2.out',onUpdate:function(){el.textContent=Math.round(o.v);}});}});});
  gsap.utils.toArray('.pbar i, .odds .bar i').forEach(function(el){
   var w=el.style.width; if(!w||w==='0%') return; el.style.width='0%';
   ST.create({trigger:el,start:'top 96%',once:true,onEnter:function(){
    gsap.to(el,{width:w,duration:.9,ease:'power2.out'});}});});
  ST.refresh();
 });
 mm.add('(prefers-reduced-motion: reduce)',function(){ gsap.set(kids,{opacity:1,y:0}); d.classList.remove('pre-anim'); });
 return true;
}
function initFX(){
 if(window.innerWidth<760||typeof window.THREE==='undefined') return;
 var THREE=window.THREE,host=document.createElement('div'); host.id='oracle-fx';
 var rnd; try{ rnd=new THREE.WebGLRenderer({antialias:false,alpha:true,powerPreference:'low-power'}); }catch(e){ return; }
 document.body.appendChild(host); host.appendChild(rnd.domElement);
 var DPR=Math.min(window.devicePixelRatio||1,1.75); rnd.setPixelRatio(DPR);
 var sc=new THREE.Scene(),cam=new THREE.OrthographicCamera(-1,1,1,-1,0,1);
 var U={u_time:{value:0},u_res:{value:new THREE.Vector2(1,1)}};
 var FRAG=[
  "precision highp float;",
  "uniform float u_time; uniform vec2 u_res;",
  "float h(vec2 p){return fract(sin(dot(p,vec2(127.1,311.7)))*43758.5453);}",
  "float vn(vec2 p){vec2 i=floor(p),f=fract(p);f=f*f*(3.0-2.0*f);",
  "return mix(mix(h(i),h(i+vec2(1,0)),f.x),mix(h(i+vec2(0,1)),h(i+vec2(1,1)),f.x),f.y);}",
  "float fbm(vec2 p){float v=0.0,a=0.5;for(int i=0;i<5;i++){v+=a*vn(p);p*=2.0;a*=0.5;}return v;}",
  "void main(){",
  "vec2 uv=gl_FragCoord.xy/u_res.xy;",
  "vec2 p=uv*vec2(u_res.x/u_res.y,1.0)*2.2;",
  "float t=u_time*0.06;",
  "vec2 q=vec2(fbm(p+vec2(0.0,t)),fbm(p+vec2(5.2,-t)));",
  "float f=fbm(p+1.8*q+t*0.5);",
  "f=smoothstep(0.25,1.05,f);",
  "vec3 base=vec3(0.039,0.047,0.039);",
  "vec3 lime=vec3(0.776,0.949,0.306);",
  "vec3 col=mix(base,lime,f*0.55);",
  "col*=0.7+0.5*f;",
  "float a=(0.30+0.55*f);",
  "a*=0.4+0.6*smoothstep(1.0,0.15,uv.y);",
  "gl_FragColor=vec4(col,a);}"
 ].join('\n');
 var mat=new THREE.ShaderMaterial({uniforms:U,transparent:true,vertexShader:'void main(){gl_Position=vec4(position,1.0);}',fragmentShader:FRAG});
 sc.add(new THREE.Mesh(new THREE.PlaneGeometry(2,2),mat));
 function size(){var w=host.clientWidth,h=host.clientHeight; rnd.setSize(w,h,false); U.u_res.value.set(Math.max(1,w*DPR),Math.max(1,h*DPR));}
 size(); window.addEventListener('resize',size);
 requestAnimationFrame(function(){host.classList.add('on');});
 var t0=performance.now(),raf=0;
 function loop(now){ U.u_time.value=(now-t0)*0.001; rnd.render(sc,cam); raf=requestAnimationFrame(loop); }
 function start(){ if(!raf) raf=requestAnimationFrame(loop); }
 function stop(){ if(raf){ cancelAnimationFrame(raf); raf=0; } }
 document.addEventListener('visibilitychange',function(){ document.hidden?stop():start(); });
 start();
}
function go(){
 try{ initMotion(); }catch(e){ document.documentElement.classList.remove('pre-anim'); document.documentElement.classList.remove('gsap-on'); }
 try{ if(!matchMedia('(prefers-reduced-motion: reduce)').matches) initFX(); }catch(e){}
}
if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',go); else go();
})();
</script>"""

def build_html(body):
    css = extract_css(DEFAULT_CSS)
    return ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>Apollo's Oracle · World Cup 2026 Predictions vs Reality</title>"
            "<link rel='preconnect' href='https://fonts.googleapis.com'>"
            "<link rel='preconnect' href='https://fonts.gstatic.com' crossorigin>"
            "<link href='https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;700&family=JetBrains+Mono:wght@400;500;600&display=swap' rel='stylesheet'>"
            + FX_HEAD +
            f"<style>{css}</style></head><body>{body}" + FX_JS + "</body></html>")

# =========================================================================== CONTROL
REQUIRED_HEADINGS = ["At a glance", "Model scorecard", "Completed matches", "Group standings", "Next up"]

def data_hash(matches, estats):
    payload = [{"id":m["id"],"g1":m["g1"],"g2":m["g2"],"st":m["statusName"]} for m in matches]
    payload.append({"e": {k: (v.get("yellow"),v.get("red"),len(v.get("scorers",[]))) for k,v in sorted(estats.items())}})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

def load_state():
    if STATE.exists():
        try: return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception: return {}
    return {}

def save_state(d): STATE.write_text(json.dumps(d, indent=2), encoding="utf-8")

def backup_index():
    if not INDEX.exists(): return
    stamp = datetime.datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S")
    (BACKUPS / f"index-{stamp}.html").write_text(INDEX.read_text(encoding="utf-8"), encoding="utf-8")
    files = sorted(BACKUPS.glob("index-*.html"))
    for f in files[:-20]: f.unlink()

def atomic_write(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)

def main():
    global SHARPEN_G
    args = set(sys.argv[1:])
    source = "espn"
    for a in list(args):
        if a.startswith("--source"):
            source = a.split("=")[-1] if "=" in a else "espn"
    if "--recalibrate" in args or not RATINGS_F.exists():
        build_ratings()
    M = load_ratings()

    matches = fetch_matches(source)
    if not matches:
        log("ABORT: zero matches parsed — not writing."); sys.exit(2)
    estats = enrich_events(matches, enable="--no-enrich" not in args)
    if "--refresh-news" in args:
        import subprocess
        try:
            subprocess.run([sys.executable, str(Path(__file__).with_name("refresh_news.py"))],
                           timeout=180, check=False)
        except Exception as e:
            log(f"warn: refresh_news failed ({e})")
    load_adjustments()
    if "--no-tune" not in args:
        tuned, ntune, sv_live = auto_tune_sv_w(M, matches)
        log(f"auto-tuned SV_W -> {tuned} (live-optimal {sv_live} over {ntune} games, shrunk to 0.25 prior)")
    df, dobs, dmod, dn = calibrate_draw_rate(M, matches)
    log(f"draw calibration -> x{df} (tournament {dobs*100:.0f}% draws vs model {dmod*100:.0f}% over {dn} games)")
    gf, gobs, gmod, gn = calibrate_goal_rate(M, matches)
    log(f"goals calibration -> x{gf} (tournament {gobs:.2f} goals/game vs model {gmod:.2f} over {gn} games)")
    compute_form(M, matches)
    _topform = sorted(FORM.items(), key=lambda x: abs(x[1]), reverse=True)[:3]
    log("in-tournament form -> " + ", ".join(f"{disp(t)} {v:+.0f}" for t, v in _topform if abs(v) >= 1))
    compute_suspensions()
    if SUSPENSIONS:
        log("suspensions -> " + ", ".join(f"{disp(t)} {v['delta']:+d}" for t, v in SUSPENSIONS.items()))
    compute_svr(M, matches)
    compute_att_def(M, matches)
    gsp, sobs, sbase, sn = calibrate_spread(M, matches)
    log(f"scoreline dispersion -> gamma {gsp} (4+ goals: tournament {sobs*100:.0f}% vs model {sbase*100:.0f}% over {sn} games)")
    stand = standings(matches)
    sharp_map, SHARPEN_G = walkforward_sharpen(M, matches)
    log(f"confidence calibration -> live sharpen exponent {SHARPEN_G} "
        f"(results-only, walk-forward, monotone -> picks unchanged)")
    pvr, summary, market = pred_vs_reality(M, matches, estats, sharp_map)
    log(f"fetched {len(matches)} matches ({sum(1 for m in matches if m['completed'])} finished); "
        f"model {summary[0]}/{summary[1]} correct, avg RPS {summary[2]:.3f}; "
        f"market {market['hits']}/{market['n']} (RPS h2h model {market['model_rps_h2h']:.3f} vs market {market['mkt_rps_h2h']:.3f})")

    h = data_hash(matches, estats)
    st = load_state()
    changed = (h != st.get("hash"))
    if "--dry-run" in args:
        print(json.dumps({"changed": changed, "matches": len(matches),
                          "finished": sum(1 for m in matches if m["completed"]),
                          "correct": summary[0], "evaluated": summary[1],
                          "avg_rps": round(summary[2],3), "hash": h[:12]}, indent=2))
        return
    if not changed and "--force" not in args and INDEX.exists():
        log("no data change — skipping write (use --force to override)."); return

    adv, champ = simulate(M, matches)
    poly = fetch_polymarket_champion()
    body = render(M, matches, stand, pvr, summary, market, adv, champ, estats, poly)
    full = build_html(body)
    missing = [hd for hd in REQUIRED_HEADINGS if f">{hd}" not in full and hd not in full]
    if missing:
        log(f"ABORT: rendered page missing sections {missing} — not writing."); sys.exit(3)

    backup_index()
    atomic_write(INDEX, full)
    save_state({"hash": h, "updated": datetime.datetime.now(LOCAL_TZ).isoformat(timespec="seconds"),
                "matches": len(matches), "finished": sum(1 for m in matches if m["completed"]),
                "correct": summary[0], "evaluated": summary[1], "avg_rps": round(summary[2],3)})
    log(f"wrote index.html ({len(full)} bytes).")

if __name__ == "__main__":
    main()
