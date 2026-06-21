#!/usr/bin/env python3
"""Dev harness: build + walk-forward backtest the Eragon WC2026 ensemble model.
Pure stdlib. Validates the model beats baselines before it goes into the dashboard.

Model = log-opinion-pool ensemble of:
  A) Elo-Poisson      : World-Football-Elo (time-decay) -> expected goals -> bivariate Poisson(l3)+DC-tau -> 1X2
  B) AttDef-Poisson   : Maher/Dixon-Coles per-team attack/defence (iterative weighted MLE) -> same goal model
Refs: World Football Elo; Maher (1982); Dixon & Coles (1997); Karlis & Ntzoufras (2003).
"""
import json, math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RESULTS = ROOT / "data" / "results.json"

# --- 63 long-run Elo priors (54 from upstream repo + 9 live-only finalists) ---
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
 # 9 live-only finalists (priors anchored from upstream frozen ratings)
 "norway":1880,"sweden":1752,"turkey":1731,"austria":1718,"iraq":1599,"uzbekistan":1633,
 "cape-verde":1599,"dr-congo":1650,"curacao":1548,
}
HOME_ADV = 75.0
BURN_IN = 150

# ---------------- Elo layer (online, time-decay weighted) ----------------
def base_k(league=""):
    n = (league or "").lower()
    if "world cup" in n and "qual" not in n: return 55
    if ("world cup" in n and "qual" in n) or "qualification" in n: return 40
    if any(s in n for s in ("copa america","euro championship","asian cup","africa cup","gold cup")): return 50
    if "nations league" in n or "nations cup" in n: return 32
    if "friendl" in n: return 18
    return 28

def gmult(gd):
    d = abs(gd)
    return 1.0 if d <= 1 else (1.5 if d == 2 else (11 + d) / 8.0)

def elo_expected(ra, rb, hb=0.0):
    return 1.0 / (1.0 + 10 ** ((rb - (ra + hb)) / 400.0))

def elo_expected_goals(rating, opp, hb=0.0):
    lam = 1.35 + (rating + hb - opp) / 400.0
    return max(0.3, min(3.5, lam))

# ---------------- bivariate Poisson + Dixon-Coles tau ----------------
def pois_pmf(k, lam):
    if lam <= 0: return 1.0 if k == 0 else 0.0
    return math.exp(-lam) * lam ** k / math.factorial(k)

def dc_tau(a, b, l1, l2, rho):
    if a == 0 and b == 0: return 1 - l1 * l2 * rho
    if a == 0 and b == 1: return 1 + l1 * rho
    if a == 1 and b == 0: return 1 + l2 * rho
    if a == 1 and b == 1: return 1 - rho
    return 1.0

def bivpois_pmf(y1, y2, l1, l2, l3):
    """Karlis-Ntzoufras bivariate Poisson PMF (l3 = shared correlation term)."""
    s = 0.0
    for k in range(min(y1, y2) + 1):
        s += (l1 ** (y1 - k) / math.factorial(y1 - k)) * \
             (l2 ** (y2 - k) / math.factorial(y2 - k)) * \
             (l3 ** k / math.factorial(k))
    return math.exp(-(l1 + l2 + l3)) * s

def match_prob(mu1, mu2, l3=0.0, rho=-0.10, maxg=8):
    """mu1,mu2 = expected goals (incl. correlation). Returns (winA, draw, winB)."""
    # bivariate Poisson uses l1=mu1-l3, l2=mu2-l3 so marginals stay mu1,mu2
    l1, l2 = max(0.05, mu1 - l3), max(0.05, mu2 - l3)
    wA = d = wB = 0.0
    for a in range(maxg + 1):
        for b in range(maxg + 1):
            p = bivpois_pmf(a, b, l1, l2, l3) * dc_tau(a, b, mu1, mu2, rho)
            if a > b: wA += p
            elif a < b: wB += p
            else: d += p
    t = wA + d + wB
    return wA / t, d / t, wB / t

# ---------------- attack/defence layer (Maher/DC, iterative weighted MLE) ----------------
def fit_attack_defence(matches, now_ts, half_life_months=18.0, iters=60):
    """Multiplicative Poisson MLE: lam_home=att_h*def_a*home, lam_away=att_a*def_h.
    Time-decay weighted. Returns (att, dfc, home_factor) dicts keyed by team id."""
    teams = set()
    rows = []
    for m in matches:
        if m.get("hg") is None or m.get("ag") is None: continue
        h = m["homeSlug"] or f"ghost:{m['homeName']}"
        a = m["awaySlug"] or f"ghost:{m['awayName']}"
        w = 0.5 ** (((now_ts - m["ts"]) / (30.44 * 86400)) / half_life_months)
        rows.append((h, a, m["hg"], m["ag"], w))
        teams.add(h); teams.add(a)
    att = {t: 1.0 for t in teams}
    dfc = {t: 1.0 for t in teams}
    home = 1.3
    for _ in range(iters):
        # home factor
        num = sum(w * hg for _, _, hg, _, w in rows)
        den = sum(w * att[h] * dfc[a] for h, a, _, _, w in rows)
        home = max(1e-6, num / den) if den else home
        gs, gc, ds_a, ds_d = {}, {}, {}, {}
        for h, a, hg, ag, w in rows:
            gs[h] = gs.get(h, 0) + w * hg
            gs[a] = gs.get(a, 0) + w * ag
            gc[h] = gc.get(h, 0) + w * ag
            gc[a] = gc.get(a, 0) + w * hg
            ds_a[h] = ds_a.get(h, 0) + w * dfc[a] * home     # att_h denom (home)
            ds_a[a] = ds_a.get(a, 0) + w * dfc[h]            # att_a denom (away)
            ds_d[a] = ds_d.get(a, 0) + w * att[h] * home     # def_a denom (vs home att)
            ds_d[h] = ds_d.get(h, 0) + w * att[a]            # def_h denom (vs away att)
        for t in teams:
            if ds_a.get(t, 0) > 0: att[t] = gs.get(t, 0) / ds_a[t]
            if ds_d.get(t, 0) > 0: dfc[t] = gc.get(t, 0) / ds_d[t]
        # normalise attack to geometric mean 1 (identifiability)
        logs = [math.log(att[t]) for t in teams if att[t] > 0]
        if logs:
            g = math.exp(sum(logs) / len(logs))
            for t in teams:
                att[t] /= g; dfc[t] *= g
    return att, dfc, home

def ad_expected_goals(att, dfc, home_factor, h, a):
    mu1 = att.get(h, 1.0) * dfc.get(a, 1.0) * home_factor
    mu2 = att.get(a, 1.0) * dfc.get(h, 1.0)
    return max(0.2, min(4.5, mu1)), max(0.2, min(4.5, mu2))

# ---------------- ensemble (log opinion pool) ----------------
def log_pool(p, q, w=0.5):
    out = [ (p[i] ** w) * (q[i] ** (1 - w)) for i in range(3) ]
    t = sum(out)
    return [x / t for x in out]

# ---------------- walk-forward backtest ----------------
def backtest(l3, rho, ens_w, refit_every=30):
    data = json.load(open(RESULTS, encoding="utf-8"))["matches"]
    R = {}
    def getR(s, nm):
        k = s or f"ghost:{nm}"
        if k not in R: R[k] = SEED.get(s, 1500) if s else 1500
        return R[k]
    def setR(s, nm, v): R[s or f"ghost:{nm}"] = v

    att = dfc = None; home_f = 1.3
    n = hit = 0; rps = rpsU = brier = logloss = 0.0
    favN = favHit = 0
    bins = [[0.0, 0.0, 0] for _ in range(10)]
    i = 0
    for m in data:
        if m.get("hg") is None or m.get("ag") is None: continue
        h, a = m["homeSlug"], m["awaySlug"]
        hk, ak = h or f"ghost:{m['homeName']}", a or f"ghost:{m['awayName']}"
        ra, rb = getR(h, m["homeName"]), getR(a, m["awayName"])
        if i >= BURN_IN:
            if att is None or (i % refit_every == 0):
                att, dfc, home_f = fit_attack_defence(data[:i], data[i]["ts"])
            # component A: Elo
            muA1 = elo_expected_goals(ra, rb, HOME_ADV)
            muA2 = elo_expected_goals(rb, ra, -HOME_ADV / 2)
            pA = match_prob(muA1, muA2, l3, rho)
            # component B: attack/defence
            muB1, muB2 = ad_expected_goals(att, dfc, home_f, hk, ak)
            pB = match_prob(muB1, muB2, l3, rho)
            probs = log_pool(list(pA), list(pB), ens_w)
            actual = 0 if m["hg"] > m["ag"] else (2 if m["hg"] < m["ag"] else 1)
            y = [1 if actual == 0 else 0, 1 if actual == 1 else 0, 1 if actual == 2 else 0]
            pred = probs.index(max(probs))
            if pred == actual: hit += 1
            brier += sum((probs[k] - y[k]) ** 2 for k in range(3))
            logloss += -math.log(max(1e-12, probs[actual]))
            rps += 0.5 * ((probs[0]-y[0])**2 + (probs[0]+probs[1]-y[0]-y[1])**2)
            u = [1/3, 1/3, 1/3]
            rpsU += 0.5 * ((u[0]-y[0])**2 + (u[0]+u[1]-y[0]-y[1])**2)
            for k in range(3):
                bi = min(9, int(probs[k] * 10))
                bins[bi][0] += probs[k]; bins[bi][1] += y[k]; bins[bi][2] += 1
            if max(probs) >= 0.5:
                favN += 1; favHit += 1 if pred == actual else 0
            n += 1
        # online Elo update
        exp = elo_expected(ra, rb, HOME_ADV)
        sc = 1.0 if m["hg"] > m["ag"] else (0.0 if m["hg"] < m["ag"] else 0.5)
        delta = base_k(m.get("leagueName")) * gmult(m["hg"] - m["ag"]) * (sc - exp)
        setR(h, m["homeName"], ra + delta); setR(a, m["awayName"], rb - delta)
        i += 1
    ece = sum(abs(b[0]/b[2] - b[1]/b[2]) * b[2] for b in bins if b[2]) / (3 * n)
    return dict(n=n, acc=hit/n, favAcc=favHit/max(1,favN), favN=favN,
                brier=brier/n, logloss=logloss/n, rps=rps/n, rpsU=rpsU/n, ece=ece, bins=bins)

if __name__ == "__main__":
    print("Tuning (l3, rho, ensemble_weight) by RPS on walk-forward OOS...\n")
    best = None
    grid = []
    for l3 in (0.0, 0.05, 0.10, 0.15):
        for rho in (-0.13, -0.10, -0.05):
            for w in (1.0, 0.7, 0.5, 0.3):
                r = backtest(l3, rho, w)
                grid.append((r["rps"], l3, rho, w, r))
                tag = f"l3={l3:.2f} rho={rho:+.2f} w={w:.1f}"
                print(f"  {tag:30s} RPS {r['rps']:.4f}  acc {r['acc']*100:4.1f}%  logloss {r['logloss']:.3f}  ECE {r['ece']*100:.1f}%")
                if best is None or r["rps"] < best[0]:
                    best = (r["rps"], l3, rho, w, r)
    _, l3, rho, w, r = best
    print(f"\nBEST: l3={l3} rho={rho} ensemble_w={w}")
    print(f"  n={r['n']}  acc={r['acc']*100:.1f}%  favAcc(p>=50%)={r['favAcc']*100:.1f}% ({r['favN']})")
    print(f"  RPS={r['rps']:.4f} (coinflip {r['rpsU']:.4f})  Brier={r['brier']:.3f}  logloss={r['logloss']:.3f}  ECE={r['ece']*100:.1f}%")
    print(f"  vs upstream repo: RPS 0.175 / logloss 0.886 / Brier 0.520 / acc 61.9% / ECE 2.3%")
    print("\n  reliability:")
    for k, b in enumerate(r["bins"]):
        if b[2]: print(f"    {k*10:2d}-{k*10+10:3d}%  said {b[0]/b[2]*100:3.0f}% -> happened {b[1]/b[2]*100:3.0f}%  (n={b[2]})")
