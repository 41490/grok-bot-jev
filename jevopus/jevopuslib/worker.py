"""`jevopus.py worker`: the background loop that keeps started work moving without the bot polling.

Each cycle: re-queue waiting_limit jobs whose reset time passed, lease queued worker jobs (like tick), run leased
single jobs (parallel across seats, one job per seat), and resume started chains / best-of-two parents.
It exits when nothing started is left to do (or with --once after one cycle). It never starts a job that was not
already routed and approved; jobs that need confirmation stay held."""
import os, subprocess, sys, time
from pathlib import Path
from . import limitfb, limits as lim
from .core import config, db, now, refresh_seats

RUN = [sys.executable, str(Path(__file__).resolve().parent.parent / "jevopus.py"), "run"]


def _spawn(ids):
    env = {**os.environ, "JEVOPUS_NO_REEXEC": "1"}
    procs = [subprocess.Popen([*RUN, i], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) for i in ids]
    for i, p in zip(ids, procs):
        out, _ = p.communicate(); print(f"--- {i} (rc={p.returncode})\n{(out or '').strip()[-1500:]}", flush=True)


def cycle(c, cfg, run_many=_spawn, say=print):
    """One pass. -> dict(ran=[ids], pending=bool, next_wake=epoch|None)."""
    from .runner import lease
    refresh_seats(c); lim.refresh(c); limitfb.requeue_due(c, say=say)
    for j in c.execute("SELECT * FROM jobs WHERE status='queued' AND route='farm' AND chain_id IS NULL AND parent_id IS NULL "
                       "ORDER BY created_at").fetchall():
        lease(c, c.execute("SELECT * FROM jobs WHERE id=?", (j["id"],)).fetchone(), cfg, quiet=True)
    singles = [r["id"] for r in c.execute("SELECT id FROM jobs WHERE status='leased' AND chain_id IS NULL AND parent_id IS NULL")]
    parents = [r["id"] for r in c.execute(
        "SELECT id FROM jobs WHERE started_at IS NOT NULL AND ((chain=1 AND status IN ('running','queued')) OR "
        "(route='best_of_two' AND status IN ('running','queued')))")]
    ran = singles + parents
    if ran: run_many(ran)
    pend = c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('waiting_limit','leased') OR "
                     "(started_at IS NOT NULL AND status IN ('running','queued') AND (chain=1 OR route='best_of_two'))").fetchone()[0]
    nxt = c.execute("SELECT MIN(wait_until) FROM jobs WHERE status='waiting_limit'").fetchone()[0]
    return {"ran": ran, "pending": bool(pend), "next_wake": nxt}


def cmd_worker(a):
    c = db(); cfg = config(); t_end = now() + float(a.max_hours) * 3600
    print(f"jevopus worker: started {time.strftime('%Y-%m-%d %H:%M %Z')} (exits when nothing started is left)", flush=True)
    while True:
        r = cycle(c, cfg)
        if a.once or not r["pending"] or now() > t_end:
            print("jevopus worker: " + ("one cycle done" if a.once else "nothing left to do" if not r["pending"] else "max hours reached")
                  + "; notices: jevopus.py notices", flush=True); return r
        wait = int(a.interval)
        if r["next_wake"] and not r["ran"]: wait = max(15, min(1800, int(r["next_wake"] - now()) + 5))
        if r["next_wake"]:
            print(f"waiting: next limit reset {lim.fmt_t(r['next_wake'])}; checking again in {wait}s", flush=True)
        time.sleep(max(5, wait))
