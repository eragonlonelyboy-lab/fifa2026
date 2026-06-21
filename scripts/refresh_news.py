#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
refresh_news.py — auto-populate the news/injury layer for Apollo's Oracle.

Pure stdlib. Reads public WC-2026 injury/team-news pages as clean text via Jina Reader
(https://r.jina.ai, free, no key), scans for the 48 finalists + injury keywords, and writes
CONSERVATIVE, BOUNDED, CITED Elo deltas into data/adjustments.json. Auto entries are flagged
"auto": true and prefixed "(auto)" so the dashboard shows their provenance. Hand-curated entries
(without "auto") are always preserved and win over auto ones.

This is best-effort: it turns headlines into small bounded nudges, not precise valuations. Review
before trusting; the dashboard shows each note + source so you can sanity-check.

  python refresh_news.py            # refresh from default sources
  python refresh_news.py --dry-run  # print the merged result, write nothing
  python refresh_news.py --source https://example.com/injuries   # add an ad-hoc source
"""
import sys, os, re, json, urllib.request, ssl, datetime
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update_dashboard import NAME, DATA, slug   # reuse the canonical 48-team list + paths

ADJ_FILE = DATA / "adjustments.json"
SSLCTX = ssl.create_default_context()

# Public injury / team-news trackers, read as clean markdown via Jina Reader (no key).
SOURCES = [
    ("Soccer26Live",  "https://soccer26live.com/world-cup-2026/injuries/"),
    ("WorldCupWiki",  "https://worldcupwiki.com/world-cup-2026-injury-list/"),
    ("ESPN",          "https://www.espn.com/soccer/story/_/id/48572979/2026-fifa-world-cup-injuries-tracker-which-stars-miss-latest-info"),
    ("Covers",        "https://www.covers.com/world-cup/injury-report-2026"),
]   # FourFourTwo dropped: prose squad-admin format caused team mis-attribution
# keyword -> bounded Elo delta. Recovery FIRST so "recovered ... had sidelined him" reads positive;
# first match wins per sentence.
RULES = [
    (r"\brecover(?:ed|ing|y)\b|\bback (?:in contention|in training|to fitness|to full fitness)\b|\bfit again\b|\bfully fit\b|\bnearing return\b|\bcleared (?:to|for|after)\b|\bavailable again\b", 6),
    (r"\bruled out\b|\bout of the (?:tournament|world cup)\b|\bwill miss\b|\bmajor blow\b|\bsidelined for\b|\bseason[- ]ending\b", -15),
    (r"\bdoubt(?:ful)?\b|\bfitness (?:concern|test|scare)\b|\binjury scare\b|\brace against time\b|\bsuspended\b|\bsuspension\b", -8),
    (r"\binjur(?:y|ed)\b|\bknock\b|\bstrain\b|\bsidelined\b", -5),
]

def clean_note(s):
    s = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", s)        # images
    s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)     # links -> text
    s = s.replace("**", "").replace("|", " ").replace("#", "")
    s = re.sub(r"\s+", " ", s).strip(" *-")
    return s

def is_noise(s):
    letters = sum(c.isalpha() for c in s)
    return len(s) < 12 or letters < len(s) * 0.5      # mostly symbols / table junk

def jina_get(url):
    req = urllib.request.Request("https://r.jina.ai/" + url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, context=SSLCTX, timeout=45) as r:
        return r.read().decode("utf-8", "ignore")

def severity(text):
    for pat, d in RULES:
        if re.search(pat, text, re.I):
            return d
    return None

def scan(text, src_name, src_url):
    """Most severe injury sentence mentioning each finalist -> bounded delta + cited note."""
    found = {}
    stamp = f"{src_name}, {datetime.date.today():%b %Y}"
    patt = {s: re.compile(r"\b" + re.escape(n) + r"\b", re.I) for s, n in NAME.items()}
    for ch in re.split(r"(?<=[.!?])\s+|\n+", text):
        if len(ch) > 400:
            continue
        note = clean_note(ch)
        if is_noise(note):
            continue
        d = severity(note)
        if d is None:
            continue
        # attribution: trust a "(Team)" parenthetical; else require exactly ONE finalist named
        targets = []
        for p in re.findall(r"\(([^)]{3,40})\)", ch):
            sl = slug(p)
            if sl in NAME and sl not in targets: targets.append(sl)
        if not targets:
            matched = [s for s, rx in patt.items() if rx.search(note)]
            if len(matched) == 1:
                # reject if the team is named only as an OPPONENT (against/before/vs Team), not the subject
                if re.search(r"(?:against|before|vs\.?|face[sd]?|hosts?|opponents?|plays?|opener\s+\w+)\s+(?:the\s+)?" + re.escape(NAME[matched[0]]), note, re.I):
                    continue
                targets = matched
            else: continue                              # 0 or ambiguous -> skip (avoid mis-attribution)
        for s in targets:
            cur = found.get(s)
            if cur is None or d < cur["delta"]:         # keep the most negative (worst news)
                found[s] = {"delta": d, "note": f"(auto) {note[:170]}", "source": stamp,
                            "url": src_url, "auto": True}
    return found

def main():
    dry = "--dry-run" in sys.argv
    srcs = list(SOURCES)
    for i, a in enumerate(sys.argv):
        if a == "--source" and i + 1 < len(sys.argv):
            srcs.append(("Custom", sys.argv[i + 1]))

    existing = {}
    if ADJ_FILE.exists():
        try: existing = json.load(open(ADJ_FILE, encoding="utf-8"))
        except Exception: existing = {}
    manual = {k: v for k, v in existing.items()
              if not (isinstance(v, dict) and v.get("auto"))}

    auto = {}
    for name, url in srcs:
        try:
            hits = scan(jina_get(url), name, url)
        except Exception as e:
            print(f"warn: {name} fetch/parse failed ({e})"); continue
        for s, entry in hits.items():
            cur = auto.get(s)
            if cur is None or entry["delta"] < cur["delta"]:
                auto[s] = entry
        print(f"{name}: {len(hits)} finalist injury mentions")

    merged = {**auto, **manual}   # manual curation always wins
    if dry:
        print(json.dumps(merged, indent=2, ensure_ascii=False)); return
    if not merged:
        print("no entries; leaving adjustments.json unchanged"); return
    json.dump(merged, open(ADJ_FILE, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
    print(f"wrote {ADJ_FILE.name} — {len(auto)} auto + {len(manual)} manual entries")

if __name__ == "__main__":
    main()
