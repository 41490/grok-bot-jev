"""Limit fallback: what happens when a seat hits its subscription / rate limit during a job.

  owner did not name a model or seat AND another connected seat can do the job
      -> the job moves to that seat (recorded in jobs.moves + route_note) and runs there;
  otherwise (only one seat connected, the owner named that model/seat, or a best-of-two candidate)
      -> status waiting_limit with the estimated reset time (jobs.wait_until / wait_source); `tick` and
         `worker` re-queue it after the reset and run it again. One notification line per pause (jobs.limit_notice,
         delivered once by `jevopus.py notices`).
Reset time sources, in order: the CLI's own error text, provider-reported limits (seat_limits), else cooldown_sec
(marked as an estimate). Nothing else is invented."""
import json, re, time
from datetime import datetime, timedelta
from . import limits as lim
from .core import iso, log, now, seat_event

UNITS = {"d": 86400, "day": 86400, "days": 86400, "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
         "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60, "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1}
MAX_WAIT = 8 * 86400


def _duration(txt):
    secs = 0
    for n, u in re.findall(r"(\d+(?:\.\d+)?)\s*(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b", txt, re.I):
        secs += float(n) * UNITS[u.lower()]
    return int(secs) or None


def parse_reset(text, t=None):
    """Reset time from a worker CLI's limit error text. -> (epoch | None, how)."""
    t = t or now(); s = text or ""
    m = re.search(r"limit reached\|(\d{10})", s)  # Claude Code: "Claude AI usage limit reached|1759999999"
    if m: return float(m[1]), "CLI message (epoch)"
    m = re.search(r'"(?:resetsAt|resets_at)"\s*:\s*(\d{10})', s)
    if m and float(m[1]) > t: return float(m[1]), "CLI message (resetsAt)"
    m = re.search(r"(?:try again|resets?|retry|available again)\s+in\s+([^.\n]{1,60})", s, re.I)
    if m:
        d = _duration(m[1])
        if d: return t + d, "CLI message (try again in ...)"
    m = re.search(r"retry-after[\"']?\s*[:=]\s*[\"']?(\d+)", s, re.I)
    if m: return t + int(m[1]), "CLI message (retry-after)"
    m = re.search(r"(?:try again|resets?|reset)\s+(?:at|on)?\s*(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)", s, re.I)
    if m:
        try: return datetime.fromisoformat(m[1].replace("Z", "+00:00")).timestamp(), "CLI message (timestamp)"
        except ValueError: pass
    m = re.search(r"(?:try again|resets?)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", s, re.I) or \
        re.search(r"(?:try again|resets?)\s+at\s+(\d{1,2}):(\d{2})()", s, re.I)
    if m:
        h, mi, ap = int(m[1]), int(m[2] or 0), (m[3] or "").lower()
        if ap == "pm" and h < 12: h += 12
        if ap == "am" and h == 12: h = 0
        if h < 24 and mi < 60:
            base = datetime.fromtimestamp(t); cand = base.replace(hour=h, minute=mi, second=0, microsecond=0)
            if cand.timestamp() <= t: cand += timedelta(days=1)
            return cand.timestamp(), "CLI message (clock time, local)"
    return None, None


def estimate_reset(c, cfg, seat, out, t=None):
    """-> (epoch, source text, is_estimate)."""
    t = t or now()
    r, how = parse_reset(out, t)
    if r and t < r <= t + MAX_WAIT: return r, how, False
    rows = [x for x in lim.provider(c, seat["id"], t) if not x["stale"] and x["resets_at"] and x["resets_at"] > t
            and (x["used_percent"] or 0) >= 95]
    if rows:
        x = max(rows, key=lambda x: x["resets_at"])
        return float(x["resets_at"]), f"provider-reported {x['window']} window ({x['used_percent']:.0f}% used)", False
    return t + int(cfg["cooldown_sec"]), f"estimate: default cooldown {int(cfg['cooldown_sec']) // 60} min (no reset time reported)", True


def owner_named(job):
    """The owner named a model or seat (pins win; such jobs are never moved)."""
    return job["model_source"] == "pinned" or bool(job["pin_seat"])


def _offers(s, model): return model in (s["models"] or s["model"] or "").split(",")


def move_targets(c, cfg, job, exclude, t=None):
    """Connected seats (enabled, not cooling down, not over the limits threshold) that can take the job.
    -> [(seat row, model)], seats offering the same model first. API-key seats only if limit_fallback.allow_api_seats."""
    t = t or now(); fb = cfg.get("limit_fallback") or {}; th = float(cfg["limits"]["skip_above_percent"])
    tier = (cfg["models"].get(job["model"] or "") or {}).get("tier") or job["tier"]
    same, other = [], []
    for s in c.execute("SELECT * FROM seats WHERE enabled=1").fetchall():
        if s["id"] in exclude or (s["cooldown_until"] and s["cooldown_until"] > t): continue
        if s["auth"] == "api_key" and not fb.get("allow_api_seats"): continue
        m = lim.max_used(c, s["id"], t)
        if m and m[0] >= th: continue
        if job["model"] and _offers(s, job["model"]): same.append((s, job["model"])); continue
        offered = [x for x in (s["models"] or s["model"] or "").split(",") if x in cfg["models"]]
        pick = next((x for x in offered if cfg["models"][x].get("tier") == tier), None) or s["model"]
        other.append((s, pick))
    return same + other


def handle_limit(c, cfg, job, seat, out, t=None):
    """Called by run when a worker hit a usage/rate limit. Updates the job + seat. -> dict(action, line, ...)."""
    t = t or now()
    reset, src, est = estimate_reset(c, cfg, seat, out, t)
    c.execute("UPDATE seats SET status='cooldown', cooldown_until=? WHERE id=?", (reset, seat["id"]))
    seat_event(c, seat["id"], "cooldown", job["id"], f"usage/rate limit; reset {lim.fmt_t(reset)} ({src})")
    moves = json.loads(job["moves"] or "[]")
    tried = {seat["id"], *(mv["from_seat"] for mv in moves)}
    fb_on = bool((cfg.get("limit_fallback") or {}).get("enabled", True))
    named, bo2 = owner_named(job), bool(job["parent_id"])
    targets = [] if (named or bo2 or not fb_on) else move_targets(c, cfg, job, tried, t)
    model = job["model"] or seat["model"]
    reset_txt = lim.fmt_t(reset) + (" (estimate)" if est else "")
    if targets:
        s2, m2 = targets[0]
        why = f"{seat['id']} ({model}) hit its usage limit; resets ~{reset_txt}; source: {src}"
        moves.append({"ts": iso(), "from_seat": seat["id"], "from_model": model, "to_seat": s2["id"], "to_model": m2, "why": why})
        note = f"LIMIT FALLBACK: {why} -> moved to {s2['id']} ({m2})"
        c.execute("UPDATE jobs SET status='queued', route='farm', seat_id=NULL, lease_until=NULL, model=?, model_source=?, "
                  "moves=?, route_note=COALESCE(route_note,'')||'; '||? WHERE id=?",
                  (m2, "limit fallback", json.dumps(moves), note, job["id"]))
        c.commit(); log({"event": "limit_fallback", "job_id": job["id"], "from": seat["id"], "to": s2["id"], "model": m2, "reset": reset})
        return {"action": "moved", "to_seat": s2["id"], "to_model": m2, "reset": reset, "line": f"{job['id']}: {note}"}
    why = (f"you asked for {job['requested_model'] or job['model'] or seat['id']}" if named else
           "it is a best-of-two candidate" if bo2 else
           "limit fallback is switched off" if not fb_on else "no other connected seat can take it")
    who = f"Chain {job['chain_id']} (step {job['chain_step']})" if job["chain_id"] else f"Job {job['id']}"
    notice = (f"{who} paused: {seat['id']} ({model}) hit its usage limit and {why}; "
              f"it restarts automatically after the reset, ~{reset_txt}.")
    c.execute("UPDATE jobs SET status='waiting_limit', lease_until=NULL, wait_until=?, wait_source=?, limit_notice=?, "
              "notice_sent=0, route_note=COALESCE(route_note,'')||'; '||? WHERE id=?",
              (reset, src, notice, f"WAITING (limit): {seat['id']} limited, retry after {reset_txt} ({src})", job["id"]))
    if job["chain_id"]:
        c.execute("UPDATE jobs SET status='waiting_limit', wait_until=? WHERE id=?", (reset, job["chain_id"]))
    c.commit(); log({"event": "waiting_limit", "job_id": job["id"], "seat": seat["id"], "reset": reset, "source": src})
    return {"action": "waiting", "reset": reset, "source": src, "line": notice}


def requeue_due(c, t=None, say=print):
    """tick/worker: waiting_limit jobs whose reset time has passed -> queued again. -> [job ids]."""
    t = t or now(); out = []
    for j in c.execute("SELECT * FROM jobs WHERE status='waiting_limit' AND chain=0 AND (wait_until IS NULL OR wait_until<=?)",
                       (t,)).fetchall():
        c.execute("UPDATE jobs SET status='queued', route=COALESCE(route,'farm'), seat_id=NULL, wait_until=NULL, "
                  "route_note=COALESCE(route_note,'')||'; limit reset -> retrying' WHERE id=?", (j["id"],))
        if j["chain_id"]: c.execute("UPDATE jobs SET status='running', wait_until=NULL WHERE id=? AND status='waiting_limit'", (j["chain_id"],))
        out.append(j["id"]); say(f"{j['id']}: limit window reset -> queued again")
    c.commit(); return out


def pending_notices(c, mark=True):
    """Undelivered one-line limit notices (each is returned exactly once when mark=True)."""
    rows = c.execute("SELECT id, limit_notice FROM jobs WHERE limit_notice IS NOT NULL AND notice_sent=0 ORDER BY created_at").fetchall()
    if mark and rows:
        c.executemany("UPDATE jobs SET notice_sent=1 WHERE id=?", [(r["id"],) for r in rows]); c.commit()
    return [r["limit_notice"] for r in rows]


def cmd_notices(a):
    from .core import db
    c = db(); lines = pending_notices(c, mark=not getattr(a, "peek", False))
    print("\n".join(lines) if lines else "no new notices")


def preflight(c, cfg, job, t=None):
    """Before leasing: every enabled seat offering the job's model is cooling down after a usage limit.
    -> None (nothing to do) | "moved" (re-assigned to another connected seat) | "waiting" (status waiting_limit)."""
    t = t or now()
    if not job["model"]: return None
    offer = [s for s in c.execute("SELECT * FROM seats WHERE enabled=1").fetchall() if _offers(s, job["model"])
             and (not job["pin_seat"] or s["id"] == job["pin_seat"])]
    if not offer or any(not (s["cooldown_until"] and s["cooldown_until"] > t) for s in offer): return None
    reset = min(s["cooldown_until"] for s in offer); ids = [s["id"] for s in offer]
    named, bo2 = owner_named(job), bool(job["parent_id"])
    fb_on = bool((cfg.get("limit_fallback") or {}).get("enabled", True))
    targets = [] if (named or bo2 or not fb_on) else move_targets(c, cfg, job, set(ids), t)
    if targets:
        s2, m2 = targets[0]; moves = json.loads(job["moves"] or "[]")
        why = f"{', '.join(ids)} is cooling down after a usage limit until ~{lim.fmt_t(reset)}"
        moves.append({"ts": iso(), "from_seat": ids[0], "from_model": job["model"], "to_seat": s2["id"], "to_model": m2, "why": why})
        c.execute("UPDATE jobs SET model=?, model_source='limit fallback', moves=?, route_note=COALESCE(route_note,'')||'; '||? WHERE id=?",
                  (m2, json.dumps(moves), f"LIMIT FALLBACK: {why} -> moved to {s2['id']} ({m2})", job["id"]))
        c.commit(); log({"event": "limit_fallback", "job_id": job["id"], "from": ids[0], "to": s2["id"], "model": m2, "before_start": True})
        return "moved"
    why = (f"you asked for {job['requested_model'] or job['model']}" if named else "it is a best-of-two candidate" if bo2 else
           "limit fallback is switched off" if not fb_on else "no other connected seat can take it")
    who = f"Chain {job['chain_id']} (step {job['chain_step']})" if job["chain_id"] else f"Job {job['id']}"
    notice = (f"{who} is waiting: {', '.join(ids)} ({job['model']}) is at its usage limit and {why}; "
              f"it starts automatically after the reset, ~{lim.fmt_t(reset)}.")
    c.execute("UPDATE jobs SET status='waiting_limit', wait_until=?, wait_source=?, limit_notice=?, notice_sent=0, "
              "route_note=COALESCE(route_note,'')||'; '||? WHERE id=?",
              (reset, "seat cooldown after a usage limit", notice, f"WAITING (limit): {', '.join(ids)} limited until ~{lim.fmt_t(reset)}", job["id"]))
    if job["chain_id"]: c.execute("UPDATE jobs SET status='waiting_limit', wait_until=? WHERE id=?", (reset, job["chain_id"]))
    c.commit(); log({"event": "waiting_limit", "job_id": job["id"], "seats": ids, "reset": reset, "before_start": True})
    return "waiting"
