"""Task chains: one task run as steps plan -> build -> review, each step a real worker job, outputs passed forward.

Parent job : chain=1, route='chain', no seat, tokens 0 (steps carry the real usage; nothing is double counted).
Steps      : <id>-plan / <id>-build / <id>-review, chain_id=<id>, dirs jobs/<id>/<step>/, normal jobs (lease, run,
             verify, limits, limit fallback). Each step's exact prompt and log are kept like any job.
Models     : owner-named model (--model) -> every step on it.
             Claude Code AND Codex connected -> plan=Claude, build=Codex, review=Claude (config chain.*),
               Jev may override a step's model; --plan-model / --build-model / --review-model pin a step.
             only one subscription connected -> every step on that seat ("single-model chain"); never fails or asks
               for a second one.
Hand-off   : plan.md is copied into the build dir; the build's files are copied into review/build/, where the
             reviewer may fix small issues. The final deliverable is review/build/ + review/review.md."""
import json, re, shutil, sys
from pathlib import Path
from . import jev, models as mdl
from .core import JOBS, band, config, enabled_models, iso, log, now, playbook, record_decisions

STEPS = ("plan", "build", "review")
PROVIDER = {"claude": "Claude Code", "codex": "Codex (ChatGPT)"}
TERMINAL_OK = ("done", "needs_review")
SKIP_COPY = re.compile(r"^(worker.*\.log|result\.json|run_console\.log)$")
DEFAULT_STEP_MODELS = {"claude": {"plan": "opus", "build": "sonnet", "review": "opus"},
                       "codex": {"plan": "gpt-6-sol", "build": "gpt-6-sol", "review": "gpt-6-sol"}}


def connected(c):
    """cli -> [enabled seat rows] (subscription seats first)."""
    out = {}
    for s in c.execute("SELECT * FROM seats WHERE enabled=1 ORDER BY auth DESC, id").fetchall():
        out.setdefault(s["cli"], []).append(s)
    return out


def _offers(seats, model): return any(model in (s["models"] or s["model"] or "").split(",") for s in seats)


def step_model(cfg, cli, step, seats):
    want = ((cfg.get("chain") or {}).get("models") or DEFAULT_STEP_MODELS).get(cli, {}).get(step)
    if want and _offers(seats, want): return want
    return seats[0]["model"]


def assign(c, cfg, pb, job, pins=None, ask=None):
    """-> dict(mode, note, steps={step: {model, why, src}}, issue). Pure decision + availability; writes nothing.
    ask(pool, job) -> {step: (model, conf)} is the Jev per-step override (tests inject it)."""
    pins, groups = pins or {}, connected(c)
    th = float(pb["thresholds"].get("min_choice_confidence", 0.55))
    if job["model_source"] == "pinned" and job["model"]:  # owner named one model for the whole chain
        av = mdl.availability(c, cfg, job["model"], job["pin_seat"])
        if not av["available"]:
            return {"mode": "needs_model", "av": av, "steps": {}, "note": f"owner-named model {job['model']} unavailable: {av['reason']}"}
        return {"mode": "owner-model", "steps": {s: {"model": job["model"], "why": "you named this model for the whole chain",
                                                    "src": "pinned"} for s in STEPS},
                "note": f"owner-named model: every step runs on {job['model']}"}
    if job["pin_seat"]:
        seat = next((s for g in groups.values() for s in g if s["id"] == job["pin_seat"]), None)
        if not seat: return {"mode": "unavailable", "steps": {}, "note": f"seat {job['pin_seat']} is not connected"}
        groups = {seat["cli"]: [seat]}
    if not groups:
        return {"mode": "unavailable", "steps": {}, "note": "no seat connected yet (run jevopus.py doctor / setup-seat); chain stays queued"}
    ccfg = cfg.get("chain") or {}
    if "claude" in groups and "codex" in groups:
        mode = "two-model"
        prov = {s: ccfg.get(s) or {"plan": "claude", "build": "codex", "review": "claude"}[s] for s in STEPS}
        steps = {s: {"model": step_model(cfg, prov[s], s, groups[prov[s]]), "src": "chain default",
                     "why": f"{PROVIDER[prov[s]]} {'plans' if s == 'plan' else 'builds' if s == 'build' else 'reviews'} (default split)"}
                 for s in STEPS}
        pool_seats = [x for g in groups.values() for x in g]
    else:
        cli = next(iter(groups)); seats = groups[cli]; mode = "single-model"
        steps = {s: {"model": step_model(cfg, cli, s, seats), "src": "single seat",
                     "why": f"only {PROVIDER.get(cli, cli)} is connected ({seats[0]['id']}), so it plans, builds and reviews"}
                 for s in STEPS}
        pool_seats = seats
    pool = {m: cfg["models"][m]["fit"] for m in enabled_models(c, cfg) if m in cfg["models"] and _offers(pool_seats, m)}
    decisions = []
    if len(pool) > 1 and pb.get("enabled", True):
        try:
            picks = (ask or (lambda p, j: jev.ask_chain(pb, j, p)))(pool, job)
            for s, (m, conf) in (picks or {}).items():
                if s in steps and m in pool:
                    applied = conf >= th and m != steps[s]["model"]
                    decisions.append((f"chain_{s}", m, conf, band(conf, pb["policy"]), applied))
                    if applied: steps[s] = {"model": m, "src": "jev", "why": f"Jev picked {m} for this step (conf {conf})"}
        except Exception as e:
            decisions = [("chain_jev", f"unavailable: {type(e).__name__}", None, None, False)]
    for s, m in pins.items():
        steps[s] = {"model": m, "src": "pinned", "why": f"you named {m} for the {s} step"}
    if mode == "two-model":
        note = "two-model chain: " + ", ".join(f"{s}={steps[s]['model']}" for s in STEPS)
    else:
        note = f"single-model chain: only {pool_seats[0]['id']} is connected, so it plans, builds and reviews itself"
    return {"mode": mode, "steps": steps, "note": note, "decisions": decisions}


def _pins(c, jid):
    return {r["k"].rsplit(":", 1)[1]: r["v"] for r in c.execute("SELECT k, v FROM meta WHERE k LIKE ?", (f"chain_pin:{jid}:%",))}


def route_chain(c, job, quiet=False, ask=None):
    cfg, pb = config(), playbook()
    started = c.execute("SELECT 1 FROM jobs WHERE chain_id=? AND status NOT IN ('queued','blocked')", (job["id"],)).fetchone()
    if started: sys.exit(f"{job['id']}: chain steps already started; not re-routing")
    # guard + complexity: one Jev call (keyword fallback)
    try: j, jev_used = jev.ask_route(pb, job, {}, [], None), True
    except Exception: j, jev_used = jev.fallback_route(job, {}, [], None), False
    irr = j["irreversible_p"] >= float(cfg["guard"]["confirm_min"])
    a = assign(c, cfg, pb, job, _pins(c, job["id"]), ask)
    c.execute("DELETE FROM jobs WHERE chain_id=? AND status IN ('queued','blocked')", (job["id"],))
    issue, status = None, "queued"
    if a["mode"] == "needs_model":
        issue = mdl.issue(c, cfg, job, job["requested_model"] or job["model"], a["av"]); status = "needs_model"
    notes = [a["note"]]
    if irr: notes.append("GUARD: irreversible/external action suspected (p=%.2f) -> " % j["irreversible_p"]
                         + ("confirmed by human" if job["confirmed"] else "REQUIRES --confirmed (jevopus.py confirm <id>)"))
    if not jev_used: notes.append("jev not used for guard -> keyword fallback")
    chain_models = " -> ".join(a["steps"][s]["model"] for s in STEPS) if a["steps"] else None
    c.execute("UPDATE jobs SET route='chain', status=?, model=?, route_note=?, chain_note=?, requires_confirm=?, complexity=?, "
              "model_issue=? WHERE id=?", (status, chain_models or job["model"], "; ".join(notes), a["note"], int(irr),
                                           j.get("complexity"), json.dumps(issue) if issue else None, job["id"]))
    for s in STEPS if a["steps"] else ():
        st = a["steps"][s]; sid = f"{job['id']}-{s}"; d = JOBS / job["id"] / s; d.mkdir(parents=True, exist_ok=True)
        g, cons, dw = step_text(job, s)
        c.execute("INSERT INTO jobs(id,goal,constraints,done_when,kind,status,created_at,route,tier,route_note,model,effort,"
                  "pin_seat,model_source,goal_hash,requires_confirm,confirmed,from_agent,forced,cli_config,attach,job_dir,"
                  "complexity,search,chain_id,chain_step,requested_model) "
                  "VALUES(?,?,?,?,?,'queued',?,'farm',?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,?)",
                  (sid, g, cons, dw, job["kind"], iso(), (cfg["models"].get(st["model"]) or {}).get("tier", "strong"),
                   f"chain {s} step of {job['id']}: {st['why']}", st["model"], job["effort"], job["pin_seat"],
                   "pinned" if st["src"] == "pinned" else f"chain: {st['src']}", job["goal_hash"], int(irr), job["confirmed"],
                   job["from_agent"], job["cli_config"], "[]", str(d), j.get("complexity"), job["search"], job["id"], s,
                   st["model"] if st["src"] == "pinned" else None))
    record_decisions(c, job["id"], [("guard", j["irreversible_p"], max(j["irreversible_p"], 1 - j["irreversible_p"]),
                                     band(max(j["irreversible_p"], 1 - j["irreversible_p"]), pb["policy"]), irr)]
                     + list(a.get("decisions") or []), jev_used)
    c.commit()
    out = {"job_id": job["id"], "route": "chain", "status": status, "mode": a["mode"], "note": a["note"],
           "steps": {s: a["steps"][s] for s in a["steps"]}, "requires_confirm": irr}
    log({"event": "route_chain", **out})
    if not quiet:
        print(json.dumps(out, indent=2))
        if issue: print("\n" + mdl.human(issue, job["id"]))
        elif a["steps"]: print(f"\nrun it with: jevopus.py run {job['id']}")
    return out


def step_text(job, step, prev=None):
    """(goal, constraints, done_when) for a step. prev: {step: result dict} of earlier steps."""
    task, cons, dw = job["goal"], (job["constraints"] or "").strip(), (job["done_when"] or "").strip()
    prev = prev or {}
    if step == "plan":
        return (f"[Chain step 1/3: PLAN] Make a concrete plan for this task; do not build it yet.\nTASK: {task}",
                (cons + " " if cons else "") + "Write the plan to ./plan.md: numbered build steps, the files/deliverables "
                "to create, approach and tools, risks, and acceptance checks a reviewer can verify. Keep it under ~150 lines.",
                "plan.md exists with numbered build steps and acceptance checks for the task" + (f" (task done-when: {dw})" if dw else ""))
    if step == "build":
        p = prev.get("plan") or {}
        return (f"[Chain step 2/3: BUILD] Build this task by following ./plan.md (written by the planning step).\nTASK: {task}\n"
                f"PLAN SUMMARY: {p.get('summary') or 'see ./plan.md'}",
                (cons + " " if cons else "") + "Put every deliverable in this directory. If the plan is wrong somewhere, "
                "deviate sensibly and say so in open_issues.",
                dw or "the deliverable described in TASK exists here and meets the acceptance checks in plan.md")
    b = prev.get("build") or {}
    return (f"[Chain step 3/3: REVIEW] Review the build in ./build/ against TASK, DONE WHEN and ./plan.md.\nTASK: {task}\n"
            f"DONE WHEN (task): {dw or 'goal met'}\nBUILD SUMMARY: {b.get('summary') or '-'}\n"
            f"BUILD OPEN ISSUES: {'; '.join(b.get('open_issues') or []) or 'none'}",
            (cons + " " if cons else "") + "Fix small, clear problems directly inside ./build/ and list anything bigger. "
            "Write ./review.md: verdict PASS or FAIL, what you checked, what you fixed, remaining issues. In result.json "
            "list review.md and every build/ file you changed.",
            "review.md exists with a PASS/FAIL verdict, the checks performed and remaining issues")


def steps(c, pid):
    rows = {r["chain_step"]: r for r in c.execute("SELECT * FROM jobs WHERE chain_id=?", (pid,)).fetchall()}
    return [rows[s] for s in STEPS if s in rows]


def _result(k):
    try: return json.loads(Path(k["result_path"]).read_text())
    except Exception: return {}


def _copy_tree(src, dst):
    for p in Path(src).rglob("*"):
        rel = p.relative_to(src)
        if p.is_dir() or SKIP_COPY.match(p.name) or str(rel) == "plan.md": continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True); shutil.copy2(p, dst / rel)


def materialize(c, parent, k, done):
    """Write the step's inputs (earlier outputs) into its dir and refresh its goal text before it runs."""
    d = Path(k["job_dir"]); d.mkdir(parents=True, exist_ok=True); prev = {s: _result(r) for s, r in done.items()}
    if k["chain_step"] in ("build", "review") and "plan" in done:
        pf = Path(done["plan"]["job_dir"]) / "plan.md"
        if pf.exists(): shutil.copy2(pf, d / "plan.md")
    if k["chain_step"] == "review" and "build" in done:
        bd = d / "build"; bd.mkdir(exist_ok=True); _copy_tree(Path(done["build"]["job_dir"]), bd)
    g, cons, dw = step_text(parent, k["chain_step"], prev)
    c.execute("UPDATE jobs SET goal=?, constraints=?, done_when=? WHERE id=?", (g, cons, dw, k["id"])); c.commit()


def run_parent(c, parent, run_fn=None):
    """Run the steps in order (resumable: finished steps are kept; a waiting step pauses the chain)."""
    from .runner import lease, cmd_run
    cfg, pid = config(), parent["id"]
    run_fn = run_fn or (lambda jid: cmd_run(type("A", (), {"job_id": jid})()))
    if parent["requires_confirm"] and not parent["confirmed"]: sys.exit("chain requires human confirmation first (jevopus.py confirm <id>)")
    if parent["status"] == "needs_model": sys.exit(f"{pid}: needs_model - see jevopus.py report {pid} (repin or cancel)")
    if not steps(c, pid):  # e.g. created before any seat was connected: assign now
        route_chain(c, parent, quiet=True); parent = c.execute("SELECT * FROM jobs WHERE id=?", (pid,)).fetchone()
        if parent["requires_confirm"] and not parent["confirmed"]: sys.exit("chain requires human confirmation first (jevopus.py confirm <id>)")
        if not steps(c, pid): sys.exit(f"{pid}: no chain steps yet ({parent['chain_note'] or parent['route_note'] or ''})")
    c.execute("UPDATE jobs SET started_at=COALESCE(started_at, ?), status='running' WHERE id=?", (iso(), pid)); c.commit()
    guard = 0
    while guard < 12:
        guard += 1; done = {}
        for k in steps(c, pid):
            if k["status"] in TERMINAL_OK: done[k["chain_step"]] = k; continue
            if k["status"] in ("failed", "blocked", "cached"):
                return finish(c, pid, failed_step=k)
            if k["status"] == "waiting_limit":
                c.execute("UPDATE jobs SET status='waiting_limit', wait_until=? WHERE id=?", (k["wait_until"], pid)); c.commit()
                print(f"{pid}: paused at step {k['chain_step']} - {k['limit_notice'] or 'waiting for a usage limit reset'}"); return "waiting"
            if k["status"] == "running":
                print(f"{pid}: step {k['chain_step']} is running elsewhere; re-run `jevopus.py run {pid}` later"); return "busy"
            if k["status"] == "queued":
                materialize(c, c.execute("SELECT * FROM jobs WHERE id=?", (pid,)).fetchone(), k, done)
                if not lease(c, c.execute("SELECT * FROM jobs WHERE id=?", (k["id"],)).fetchone(), cfg):
                    print(f"{pid}: step {k['chain_step']} waiting for a free seat; re-run `jevopus.py run {pid}` (or jevopus.py worker)")
                    return "waiting"
            print(f"{pid}: step {k['chain_step']} ({k['model']})", flush=True)
            run_fn(k["id"])
            break  # re-read all steps after each run (the step may have moved seats or paused)
        else:
            return finish(c, pid)
    return "stopped"


def finish(c, pid, failed_step=None):
    parent = c.execute("SELECT * FROM jobs WHERE id=?", (pid,)).fetchone(); ks = steps(c, pid)
    if failed_step is not None: status = "failed"
    elif all(k["status"] == "done" for k in ks): status = "done"
    else: status = "needs_review"
    base = JOBS / pid; base.mkdir(parents=True, exist_ok=True)
    rv = next((k for k in ks if k["chain_step"] == "review"), None); files, summary, issues = [], "", []
    last = rv if rv is not None and rv["status"] in TERMINAL_OK else next((k for k in reversed(ks) if k["status"] in TERMINAL_OK), None)
    if last is not None:
        r = _result(last); summary = r.get("summary") or ""; issues = r.get("open_issues") or []
        d = Path(last["job_dir"])
        files = [str((d / f).relative_to(base)) if (d / f).resolve().is_relative_to(base.resolve()) else str(d / f) for f in r.get("files") or []]
        if last["chain_step"] == "review" and (d / "build").exists():
            files += [str(p.relative_to(base)) for p in sorted((d / "build").rglob("*")) if p.is_file() and not SKIP_COPY.match(p.name)
                      and str(p.relative_to(base)) not in files]
    if failed_step is not None:
        summary = f"chain stopped: step {failed_step['chain_step']} ended {failed_step['status']}. " + (_result(failed_step).get("summary") or "")
    res = {"status": "failed" if status == "failed" else (_result(last).get("status") if last is not None else None) or "done",
           "summary": summary[:600], "files": files, "open_issues": issues}
    (base / "result.json").write_text(json.dumps(res, indent=2))
    summ = {"chain": pid, "status": status, "note": parent["chain_note"], "finished_at": iso(),
            "steps": [{"step": k["chain_step"], "job": k["id"], "model": k["model"], "seat": k["seat_id"], "status": k["status"],
                       "verify_status": k["verify_status"], "tokens": k["tokens"] or 0, "secs": k["secs"],
                       "moves": json.loads(k["moves"] or "[]")} for k in ks]}
    (base / "chain.json").write_text(json.dumps(summ, indent=2))
    secs = None
    if parent["started_at"]:
        from datetime import datetime
        try: secs = int(now() - datetime.fromisoformat(parent["started_at"]).timestamp())
        except Exception: pass
    vs = "pass" if status == "done" else "fail"
    c.execute("UPDATE jobs SET status=?, result_path=?, finished_at=?, secs=?, verify_status=?, wait_until=NULL WHERE id=?",
              (status, str(base / "result.json"), iso(), secs, vs, pid)); c.commit()
    log({"event": "chain_done", "job_id": pid, "status": status, "steps": [(s["step"], s["model"], s["status"]) for s in summ["steps"]]})
    print(f"chain {pid}: {status} ({parent['chain_note']}); see jevopus.py report {pid}")
    return status
