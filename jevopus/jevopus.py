#!/usr/bin/env python3
"""Jevopus v2.7 (formerly Farm): worker router. Jev (TypeSafe) judges; code owns leases, runs, policy. Secrets never printed.

  jevopus.py submit --goal G [--constraints C] [--done-when D] [--kind K] [--model M] [--effort E] [--seat S]
                 [--recipe NAME --var k=v ...] [--from-agent NAME] [--confirmed] [--force] [--best-of-two] [--search]
  jevopus.py route <id> | tick | run <id> | confirm <id> | cancel <id> | status
  jevopus.py report <id> | usage [--since 7d] | weekly | feedback <id> ok|wrong [note]
  jevopus.py recipes | doctor | setup-seat codex-sub|codex-api|claude-strong [--disable] | init
  jevopus.py limits | jev-mode [shadow|active] [--by NAME] [--note TEXT] [--force]
  jevopus.py models | guide | repin <id> <model|auto> | repin <id> --alternative [N]
  jevopus.py chain "<task>" [--done-when D] [--model M] [--plan-model M --build-model M --review-model M] [--run]
  jevopus.py suggest "<request>" [--json] | suggest --declined "<request>" | notices [--peek]
  jevopus.py worker [--once] [--interval 60] [--max-hours 24]
"""
import argparse, os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from jevopuslib.core import jev_dir  # noqa: E402  (stdlib-only import, safe before re-exec)

try:
    import yaml, typesafe_sdk  # noqa: F401
except ImportError:  # re-exec under the Jev router venv (typesafe-sdk + pyyaml)
    d = jev_dir(); vpy = str(d / ".venv/bin/python") if d else None
    if vpy and os.path.realpath(sys.prefix) != os.path.realpath(str(d / ".venv")) and not (os.environ.get("JEVOPUS_NO_REEXEC") or os.environ.get("FARM_NO_REEXEC")):
        os.environ["JEVOPUS_NO_REEXEC"] = "1"; os.execv(vpy, [vpy, *sys.argv])
    if len(sys.argv) < 2 or sys.argv[1] not in ("doctor", "init", "status", "recipes", "limits", "jev-mode", "models", "guide",
                                                "suggest", "notices"):
        sys.exit("Jev router venv not found (need grok-bot-jev/.venv with typesafe-sdk). Run jevopus_install.sh or jevopus.py doctor.")

from jevopuslib import guide, limitfb, limits, models, reports, runner, setup, suggest, worker  # noqa: E402
from jevopuslib.core import DB, config, db  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description="Jevopus worker router (v2.7)"); sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("init", "tick", "status", "weekly", "recipes", "doctor", "limits", "models", "guide"): sp.add_parser(n)
    s = sp.add_parser("submit")
    s.add_argument("--goal", default=""); s.add_argument("--constraints", default=""); s.add_argument("--done-when", default="")
    s.add_argument("--kind", default="coding")
    s.add_argument("--model", default="", help="pin model (else Jev picks among enabled seats' models)")
    s.add_argument("--effort", default="", help="codex model_reasoning_effort (low|medium|high)")
    s.add_argument("--seat", default="", help="pin to this seat (user-requested); never moved to another login")
    s.add_argument("--recipe", default=""); s.add_argument("--var", action="append", default=[], help="recipe var k=v")
    s.add_argument("--from-agent", default="", help="name of the bot/agent that sent this job")
    s.add_argument("--confirmed", action="store_true", help="human confirmed irreversible/external actions")
    s.add_argument("--force", action="store_true", help="skip cache reuse")
    s.add_argument("--search", action="store_true", help="enable live web search for the worker (codex web_search=live; claude WebSearch/WebFetch)")
    s.add_argument("--best-of-two", action="store_true", help="run on two models (prefer two seats), keep the better verified result")
    s.add_argument("--chain", action="store_true", help="run as a task chain: plan -> build -> review (route assigns step models)")
    for st in ("plan", "build", "review"): s.add_argument(f"--{st}-model", default="", help=f"chain: pin the {st} step's model")
    ch = sp.add_parser("chain", help="multi-step job: plan -> build -> review, outputs passed forward")
    ch.add_argument("task"); ch.add_argument("--constraints", default=""); ch.add_argument("--done-when", default="")
    ch.add_argument("--kind", default="coding"); ch.add_argument("--model", default="", help="owner-named model for every step")
    ch.add_argument("--seat", default=""); ch.add_argument("--effort", default="")
    for st in ("plan", "build", "review"): ch.add_argument(f"--{st}-model", default="", help=f"pin the {st} step's model")
    ch.add_argument("--from-agent", default=""); ch.add_argument("--confirmed", action="store_true")
    ch.add_argument("--search", action="store_true"); ch.add_argument("--run", action="store_true", help="run all steps now (foreground)")
    sg = sp.add_parser("suggest", help="in-chat advice: seat or Grok, which connected model, ~time, one-line offer")
    sg.add_argument("text", nargs="+"); sg.add_argument("--json", action="store_true")
    sg.add_argument("--declined", action="store_true", help="owner said no: don't offer a seat for this kind of task for a while")
    nt = sp.add_parser("notices", help="undelivered one-line limit notices (each shown once)"); nt.add_argument("--peek", action="store_true")
    wk = sp.add_parser("worker", help="background loop: retries after limit resets, resumes chains, runs leased jobs")
    wk.add_argument("--once", action="store_true"); wk.add_argument("--interval", type=int, default=60)
    wk.add_argument("--max-hours", type=float, default=24)
    for n in ("route", "run", "report", "confirm", "cancel"): sp.add_parser(n).add_argument("job_id")
    sp.add_parser("usage").add_argument("--since", default="7d")
    f = sp.add_parser("feedback"); f.add_argument("job_id"); f.add_argument("verdict", choices=["ok", "wrong"]); f.add_argument("note", nargs="*")
    jm = sp.add_parser("jev-mode"); jm.add_argument("mode", nargs="?", choices=["shadow", "active"])
    jm.add_argument("--by", default="", help="who is changing it (default: $JEVOPUS_ACTOR or OS user)")
    jm.add_argument("--note", default=""); jm.add_argument("--force", action="store_true", help="switch to active below the accuracy threshold")
    rp = sp.add_parser("repin", help="needs_model job: use another model / an offered alternative / auto")
    rp.add_argument("job_id"); rp.add_argument("model", nargs="?", default="")
    rp.add_argument("--alternative", type=int, nargs="?", const=1, default=0, help="use offered alternative N (default 1)")
    ss = sp.add_parser("setup-seat"); ss.add_argument("seat"); ss.add_argument("--disable", action="store_true")
    a = ap.parse_args()
    {"init": lambda a: (db(), config(), print(f"db ready: {DB}; config: {len(config()['models'])} models")), "submit": runner.cmd_submit, "route": runner.cmd_route,
     "tick": runner.cmd_tick, "run": runner.cmd_run, "confirm": runner.cmd_confirm, "cancel": runner.cmd_cancel, "status": reports.cmd_status,
     "report": reports.cmd_report, "usage": reports.cmd_usage, "weekly": reports.cmd_weekly,
     "feedback": reports.cmd_feedback, "recipes": reports.cmd_recipes, "doctor": setup.cmd_doctor,
     "setup-seat": setup.cmd_setup_seat, "limits": limits.cmd_limits, "jev-mode": reports.cmd_jev_mode,
     "models": models.cmd_models, "guide": guide.cmd_guide, "repin": runner.cmd_repin, "chain": cmd_chain,
     "suggest": suggest.cmd_suggest, "notices": limitfb.cmd_notices, "worker": worker.cmd_worker}[a.cmd](a)


def cmd_chain(a):
    """chain = submit --chain + route (assigns each step's model); --run also runs it now."""
    a.goal, a.recipe, a.var, a.force, a.best_of_two, a.chain, a.quiet = a.task, "", [], False, False, True, True
    jid = runner.cmd_submit(a)
    print(f"chain job: {jid}")
    runner.cmd_route(type("A", (), {"job_id": jid})())
    if a.run: runner.cmd_run(type("A", (), {"job_id": jid})())


if __name__ == "__main__":
    main()
