"""`jevopus.py suggest "<request>"`: should a request from normal chat go to a seat, which connected model fits,
roughly how long, and a one-line offer for the owner. Fast and offline (keyword rules + this install's own job
history; no model call, no seat used). Only connected seats are ever recommended.

`suggest --declined "<request>"` records a no for that kind of task: the same kind is not offered again for
suggest.decline_days (default 7) days."""
import json, re, statistics, time
from .core import config, db, now, refresh_seats

# kind: (keywords, preferred provider, (min, max) minutes, claude model, codex model, always heavy?)
KINDS = {
    "build":    (r"\b(build|create|make|develop|implement|code|program|app|application|website|web ?site|landing page|"
                 r"dashboard|bot|script|tool|api|scraper|extension|plugin|game|prototype|mvp|frontend|backend|full.?stack)\b",
                 "codex", (10, 25), "sonnet", "gpt-6-sol", True),
    "data":     (r"\b(csv|spreadsheet|excel|xlsx|sheet|dataset|data ?set|clean ?up|cleanup|dedupe|deduplicate|parse|convert|"
                 r"merge|json|sql|etl|normalize|scrape|table|pivot|process (the|this|these|my) (data|files?))\b",
                 "codex", (5, 15), "sonnet", "gpt-6-astra", False),
    "research": (r"\b(research|investigate|deep dive|market (map|analysis|research)|competitor|competitive|landscape|"
                 r"compare|comparison|sources|literature|due diligence|find (all|every|companies|leads)|lead list|study)\b",
                 "claude", (10, 20), "opus", "gpt-6-sol", False),
    "review":   (r"\b(review|audit|refactor|critique|code review|security review|proofread|check (my|this|the) (code|repo|pr)|"
                 r"find bugs|debug)\b", "claude", (5, 15), "opus", "gpt-6-sol", False),
    "document": (r"\b(report|whitepaper|white paper|e-?book|proposal|business plan|documentation|docs|manual|guide|"
                 r"presentation|deck|slides|article|essay|long.?form|chapter|pitch deck|media kit|playbook|strategy)\b",
                 "claude", (10, 20), "opus", "gpt-6-sol", False),
    "video":    (r"\b(video|animation|motion|mp4|render)\b", "codex", (10, 20), "sonnet", "gpt-6-sol", True),
}
HEAVY_HINTS = re.compile(r"\b(full|complete|entire|whole|detailed|in.?depth|comprehensive|thorough|long|multi.?(page|step|file)|"
                         r"all (of )?(the|my)|end.?to.?end|production|\d{3,} ?(words|rows|lines)|\d+ ?pages|\d{2,} (items|companies|leads))\b", re.I)
LIGHT_HINTS = re.compile(r"\b(quick|quickly|short|brief|one.?line|tl;?dr|summari[sz]e (this|it)|translate|what is|what's|who is|"
                         r"when is|how do i|define|explain|remind|tweet|rephrase|fix (this|the) typo|small)\b", re.I)
PROVIDER = {"claude": "Claude Code", "codex": "ChatGPT Codex"}


def _declines(c):
    r = c.execute("SELECT v FROM meta WHERE k='suggest_declines'").fetchone()
    try: return json.loads(r[0]) if r else {}
    except Exception: return {}


def classify(text):
    t = (text or "").lower(); hits = {k: len(re.findall(v[0], t)) for k, v in KINDS.items()}
    hits = {k: n for k, n in hits.items() if n}
    kind = max(hits, key=lambda k: (hits[k], -list(KINDS).index(k))) if hits else None
    words = len(t.split())
    heavy_hint, light_hint = bool(HEAVY_HINTS.search(t)), bool(LIGHT_HINTS.search(t))
    if not kind: heavy = heavy_hint and words > 25
    elif light_hint and not heavy_hint: heavy = False
    else: heavy = KINDS[kind][5] or heavy_hint or words > 30 or len(hits) >= 2
    if t.rstrip().endswith("?") and words < 15 and not heavy_hint: heavy = False
    return kind, heavy, sorted(hits)


def _typical_minutes(c, model, default):
    secs = [r[0] for r in c.execute("SELECT secs FROM jobs WHERE model=? AND status='done' AND secs IS NOT NULL "
                                    "ORDER BY created_at DESC LIMIT 20", (model,))]
    if len(secs) >= 3:
        m = max(1, round(statistics.median(secs) / 60)); return (m, m + max(2, m // 2)), "median of your last jobs on this model"
    return default, "typical for this kind of task"


def suggest(c, cfg, text, t=None):
    t = t or now()
    kind, heavy, kinds = classify(text)
    seats = c.execute("SELECT * FROM seats WHERE enabled=1 ORDER BY auth DESC, id").fetchall()
    by_cli = {}
    for s in seats: by_cli.setdefault(s["cli"], []).append(s)
    out = {"heavy": heavy, "kind": kind, "kinds_seen": kinds, "handler": "grok", "provider": None, "model": None, "seat": None,
           "est_minutes": None, "est_text": None, "use_chain": False, "offer": None,
           "connected": [s["id"] for s in seats], "declined_before": False}
    if not heavy:
        out["reason"] = "quick or conversational request; Grok handles it directly (no seat)"; return out
    if not seats:
        out["reason"] = "heavy, but no seat is connected; Grok does what it can (setup: jevopus.py setup-seat ...)"; return out
    dec = _declines(c).get(kind or "other")
    if dec and t - dec < float((cfg.get("suggest") or {}).get("decline_days", 7)) * 86400:
        out.update(declined_before=True, reason=f"owner declined a seat for '{kind}' tasks recently; don't offer again"); return out
    k = KINDS.get(kind) or (None, "claude" if "claude" in by_cli else "codex", (10, 20), "opus", "gpt-6-sol", False)
    pref, rng = k[1], k[2]
    both = "claude" in by_cli and "codex" in by_cli
    if both and len(kinds) >= 2 and {KINDS[x][1] for x in kinds} == {"claude", "codex"}:
        cli, jev = None, True  # mixed kind: let Jev pick among all connected models
    else:
        cli, jev = (pref if pref in by_cli else next(iter(by_cli))), False
    if cli:
        want = k[3] if cli == "claude" else k[4]
        seat = next((s for s in by_cli[cli] if want in (s["models"] or "").split(",")), by_cli[cli][0])
        model = want if want in (seat["models"] or "").split(",") else seat["model"]
        rng, basis = _typical_minutes(c, model, rng)
        label = f"{PROVIDER[cli]} ({model})"
        out.update(provider=cli, model=model, seat=seat["id"])
        cool = seat["cooldown_until"] and seat["cooldown_until"] > t
    else:
        label, basis, cool = "a seat (Jev picks Claude Code or Codex)", "typical for this kind of task", False
        out.update(provider="jev picks", model="jev picks", seat=",".join(s["id"] for s in seats))
    chain = kind == "build" and (bool(HEAVY_HINTS.search(text or "")) or len((text or "").split()) > 40)
    if chain: rng = (rng[0] + 5, rng[1] + 15)
    mid = round((rng[0] + rng[1]) / 2)
    out.update(handler="seat", est_minutes=list(rng), est_text=f"~{mid} min ({rng[0]}-{rng[1]}, {basis})", use_chain=chain)
    how = " as a plan -> build -> review chain" if chain else ""
    wait = f" (that seat is cooling down after a usage limit until {time.strftime('%H:%M', time.localtime(seat['cooldown_until']))}; it would start then)" if cli and cool else ""
    out["offer"] = f"This is better on {label}{how}, ~{mid} min{wait}. Run it?"
    out["reason"] = f"{kind or 'heavy'} task; " + ("mixed kind, both providers fit -> Jev picks" if jev else
                    f"{PROVIDER[cli]} fits {kind or 'it'}" + ("" if cli == pref else " (the connected seat; it handles this kind too)"))
    q = (text or "").replace('"', "'")[:300]
    out["command"] = f'jevopus.py chain "{q}"' if chain else f'jevopus.py submit --goal "{q}"  (then route/tick/run)'
    return out


def cmd_suggest(a):
    c = db(); cfg = config(); refresh_seats(c)
    text = " ".join(a.text) if isinstance(a.text, list) else a.text
    if getattr(a, "declined", False):
        kind = classify(text)[0] or "other"; d = _declines(c); d[kind] = now()
        c.execute("INSERT OR REPLACE INTO meta VALUES('suggest_declines', ?)", (json.dumps(d),)); c.commit()
        print(f"noted: no seat offers for '{kind}' tasks for {cfg.get('suggest', {}).get('decline_days', 7)} days"); return
    r = suggest(c, cfg, text)
    if getattr(a, "json", False): print(json.dumps(r, indent=2)); return
    if r["handler"] == "grok": print(f"grok: {r['reason']}")
    else: print(f"{r['offer']}\n  kind={r['kind']} model={r['model']} seat={r['seat']} est={r['est_text']}\n  why: {r['reason']}\n  run: {r['command']}")
