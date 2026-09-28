"""Unit tests for Jevopus v2.7: task chains, limit fallback / waiting_limit, suggest, schema migration.
No live model calls: worker CLIs are replaced by a fake _exec and every Jev call is forced onto its fallback.
Run: python3 -m unittest discover -s jevopus/tests -v"""
import argparse, contextlib, io, json, os, shutil, sqlite3, sys, tempfile, time, unittest
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="jevopus-test-"))
os.environ["JEVOPUS_HOME"] = str(TMP); os.environ["JEVOPUS_NO_REEXEC"] = "1"; os.environ["JEVOPUS_JEV_DIR"] = str(TMP / "nojev")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from jevopuslib import chain, core, jev, limitfb, reports, runner, suggest, worker  # noqa: E402

LIMIT_MSG = "ERROR: You've hit your usage limit. Upgrade to Pro or try again in 2 hours 5 minutes."


def _nojev(*a, **k): raise RuntimeError("no Jev in tests")


class Fake:
    """Stands in for the codex/claude CLIs. behavior[seat_id] = list of 'ok' | 'limit' (consumed in order; last repeats)."""
    def __init__(self): self.behavior, self.calls = {}, []

    def __call__(self, argv, jdir, env, timeout):
        home = env.get("CODEX_HOME") or env.get("CLAUDE_CONFIG_DIR")
        seat = Path(home).parent.name
        seq = self.behavior.get(seat, ["ok"]); b = seq.pop(0) if len(seq) > 1 else seq[0]
        model = argv[argv.index("-m") + 1] if "-m" in argv else argv[argv.index("--model") + 1]
        self.calls.append((seat, model, str(jdir)))
        if b == "limit": return LIMIT_MSG + "\n--- stderr ---\n", 1
        jdir = Path(jdir)
        files = ["out.txt"]
        (jdir / "out.txt").write_text(f"made by {seat}/{model}\n")
        prompt = argv[-1] if seat.startswith("codex") else argv[argv.index("-p") + 1]
        if "PLAN]" in prompt: (jdir / "plan.md").write_text("1. do it\nacceptance: out.txt exists\n"); files.append("plan.md")
        if "REVIEW]" in prompt: (jdir / "review.md").write_text("verdict: PASS\n"); files.append("review.md")
        (jdir / "result.json").write_text(json.dumps({"status": "done", "summary": f"ok from {seat}", "files": files, "open_issues": []}))
        return f"tokens used\n1,000\n--- stderr ---\n", 0


def ns(**kw):
    d = dict(goal="", constraints="", done_when="", kind="coding", model="", effort="", seat="", recipe="", var=[],
             from_agent="", confirmed=False, force=False, search=False, best_of_two=False, chain=False, quiet=True,
             plan_model="", build_model="", review_model="")
    d.update(kw); return argparse.Namespace(**d)


class Base(unittest.TestCase):
    def setUp(self):
        for p in ("jevopus.db", "config.json"): (TMP / p).unlink(missing_ok=True)
        shutil.rmtree(TMP / "jobs", ignore_errors=True)
        self._saved = {n: getattr(jev, n) for n in ("ask_route", "ask_verify", "ask_chain", "ask_second", "ask_compare")}
        for n in self._saved: setattr(jev, n, _nojev)
        self.fake = Fake(); self._exec = runner._exec; runner._exec = self.fake
        self.c = core.db(); self.cfg = core.config()

    def tearDown(self):
        for n, f in self._saved.items(): setattr(jev, n, f)
        runner._exec = self._exec

    def enable(self, *ids):
        self.c.execute("UPDATE seats SET enabled=0"); self.c.executemany("UPDATE seats SET enabled=1 WHERE id=?", [(i,) for i in ids])
        self.c.commit()

    def job(self, jid): return self.c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()

    def quiet(self, fn, *a):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf): r = fn(*a)
        return r, buf.getvalue()

    def submit(self, **kw):
        jid, _ = self.quiet(runner.cmd_submit, ns(**kw)); return jid

    def new_chain(self, task="Build a small python CLI tool that converts CSV files to JSON with tests", **kw):
        jid = self.submit(goal=task, chain=True, **kw)
        out, _ = self.quiet(chain.route_chain, self.c, self.job(jid))
        return jid, out

    def single_job(self, **kw):
        jid = self.submit(goal="Write a python script that converts a folder of CSV files into one JSON file with a summary", **kw)
        self.quiet(runner.cmd_route, argparse.Namespace(job_id=jid)); self.quiet(runner.cmd_tick, None)
        return jid


class TestChain(Base):
    def test_chain_two_seats(self):
        self.enable("codex-sub", "claude-strong")
        jid, out = self.new_chain()
        self.assertEqual(out["mode"], "two-model")
        self.assertEqual({s: out["steps"][s]["model"] for s in chain.STEPS}, {"plan": "opus", "build": "gpt-6-sol", "review": "opus"})
        st, _ = self.quiet(chain.run_parent, self.c, self.job(jid))
        self.assertEqual(st, "done")
        ks = {k["chain_step"]: k for k in chain.steps(self.c, jid)}
        self.assertEqual([ks[s]["seat_id"] for s in chain.STEPS], ["claude-strong", "codex-sub", "claude-strong"])
        self.assertTrue(all(k["status"] == "done" and k["prompt"] for k in ks.values()))
        self.assertIn("[Chain step 2/3: BUILD]", ks["build"]["prompt"])
        self.assertIn("PLAN SUMMARY: ok from claude-strong", ks["build"]["prompt"])      # output passed forward
        self.assertTrue((Path(ks["build"]["job_dir"]) / "plan.md").exists())
        self.assertTrue((Path(ks["review"]["job_dir"]) / "build" / "out.txt").exists())  # build handed to the reviewer
        self.assertEqual(self.job(jid)["status"], "done")
        _, rep = self.quiet(reports.cmd_report, argparse.Namespace(job_id=jid))
        self.assertIn("two-model chain", rep); self.assertEqual(rep.count("exact prompt sent:"), 3)
        res = json.loads(Path(self.job(jid)["result_path"]).read_text())
        self.assertIn("review/build/out.txt", res["files"])

    def test_chain_one_seat(self):
        self.enable("codex-sub")
        jid, out = self.new_chain()
        self.assertEqual(out["mode"], "single-model")
        self.assertIn("single-model chain", out["note"])
        self.assertNotIn("setup-seat", out["note"]); self.assertNotIn("connect claude", out["note"].lower())
        self.assertEqual({out["steps"][s]["model"] for s in chain.STEPS}, {"gpt-6-sol"})
        st, _ = self.quiet(chain.run_parent, self.c, self.job(jid))
        self.assertEqual(st, "done")
        self.assertEqual({k["seat_id"] for k in chain.steps(self.c, jid)}, {"codex-sub"})
        _, rep = self.quiet(reports.cmd_report, argparse.Namespace(job_id=jid))
        self.assertIn("single-model chain", rep)

    def test_chain_one_claude_seat(self):
        self.enable("claude-strong")
        jid, out = self.new_chain()
        self.assertEqual(out["mode"], "single-model")
        self.assertEqual({out["steps"][s]["model"] for s in chain.STEPS} <= {"opus", "sonnet"}, True)
        st, _ = self.quiet(chain.run_parent, self.c, self.job(jid))
        self.assertEqual(st, "done")

    def test_chain_created_before_any_seat(self):
        self.enable()
        jid, out = self.new_chain()
        self.assertEqual((out["mode"], out["status"]), ("unavailable", "queued"))
        self.enable("codex-sub")
        st, _ = self.quiet(chain.run_parent, self.c, self.job(jid))
        self.assertEqual(st, "done")

    def test_chain_owner_model_whole_chain(self):
        self.enable("codex-sub", "claude-strong")
        jid, out = self.new_chain(model="sonnet")
        self.assertEqual(out["mode"], "owner-model")
        self.assertEqual({out["steps"][s]["model"] for s in chain.STEPS}, {"sonnet"})
        self.assertTrue(all(k["model_source"] == "pinned" for k in chain.steps(self.c, jid)))

    def test_chain_owner_model_not_connected(self):
        self.enable("codex-sub")
        jid, out = self.new_chain(model="opus")
        self.assertEqual(out["status"], "needs_model")
        self.assertEqual(chain.steps(self.c, jid), [])

    def test_chain_step_pin_and_jev_override(self):
        self.enable("codex-sub", "claude-strong")
        jid = self.submit(goal="Build a web app", chain=True, review_model="gpt-6-sol")
        out = chain.assign(self.c, self.cfg, core.playbook(), self.job(jid), chain._pins(self.c, jid),
                           ask=lambda pool, j: {"plan": ("sonnet", 0.9), "build": ("haiku", 0.2)})
        self.assertEqual(out["steps"]["plan"]["model"], "sonnet")        # confident Jev override applied
        self.assertEqual(out["steps"]["build"]["model"], "gpt-6-sol")    # low confidence -> default kept
        self.assertEqual(out["steps"]["review"]["model"], "gpt-6-sol")   # owner step pin wins


class TestLimits(Base):
    def test_fallback_two_seats(self):
        self.enable("codex-sub", "claude-strong")
        self.fake.behavior = {"codex-sub": ["limit"], "claude-strong": ["ok"]}
        jid = self.single_job()
        self.assertEqual(self.job(jid)["seat_id"], "codex-sub")
        t0 = time.time(); self.quiet(runner.cmd_run, argparse.Namespace(job_id=jid))
        j = self.job(jid)
        self.assertEqual((j["status"], j["seat_id"]), ("done", "claude-strong"))
        mv = json.loads(j["moves"]); self.assertEqual((mv[0]["from_seat"], mv[0]["to_seat"]), ("codex-sub", "claude-strong"))
        self.assertIn("usage limit", mv[0]["why"])
        s = self.c.execute("SELECT * FROM seats WHERE id='codex-sub'").fetchone()
        self.assertEqual(s["status"], "cooldown"); self.assertAlmostEqual(s["cooldown_until"], t0 + 7500, delta=30)
        self.assertEqual(limitfb.pending_notices(self.c), [])  # moved: nothing to wait for, no notice
        _, rep = self.quiet(reports.cmd_report, argparse.Namespace(job_id=jid)); self.assertIn("LIMIT FALLBACK", rep)
        self.assertTrue(list(Path(core.job_dir(j)).glob("worker_limited_codex-sub-*.log")))

    def test_wait_one_seat_then_retry(self):
        self.enable("codex-sub")
        self.fake.behavior = {"codex-sub": ["limit", "ok"]}
        jid = self.single_job(); t0 = time.time()
        self.quiet(runner.cmd_run, argparse.Namespace(job_id=jid))
        j = self.job(jid)
        self.assertEqual(j["status"], "waiting_limit"); self.assertAlmostEqual(j["wait_until"], t0 + 7500, delta=30)
        self.assertIn("try again in", j["wait_source"])
        n1, n2 = limitfb.pending_notices(self.c), limitfb.pending_notices(self.c)
        self.assertEqual(len(n1), 1); self.assertEqual(n2, [])  # one message, not repeated
        self.assertIn("restarts automatically", n1[0])
        _, st = self.quiet(reports.cmd_status, None); self.assertIn("WAITING (limit)", st)
        self.assertEqual(limitfb.requeue_due(self.c, say=lambda m: None), [])  # before reset: stays waiting
        self.c.execute("UPDATE jobs SET wait_until=? WHERE id=?", (time.time() - 1, jid))
        self.c.execute("UPDATE seats SET cooldown_until=? WHERE id='codex-sub'", (time.time() - 1,)); self.c.commit()
        r, _ = self.quiet(worker.cycle, self.c, self.cfg, lambda ids: [runner.cmd_run(argparse.Namespace(job_id=i)) for i in ids])
        self.assertEqual(r["ran"], [jid]); self.assertEqual(self.job(jid)["status"], "done")

    def test_owner_named_model_waits_even_with_two_seats(self):
        self.enable("codex-sub", "claude-strong")
        self.fake.behavior = {"codex-sub": ["limit"]}
        jid = self.single_job(model="gpt-6-astra")
        self.quiet(runner.cmd_run, argparse.Namespace(job_id=jid))
        j = self.job(jid)
        self.assertEqual(j["status"], "waiting_limit"); self.assertIsNone(j["moves"])
        self.assertIn("you asked for gpt-6-astra", j["limit_notice"])

    def test_chain_single_seat_limit_pauses_and_resumes(self):
        self.enable("codex-sub")
        self.fake.behavior = {"codex-sub": ["ok", "limit", "ok"]}
        jid, _ = self.new_chain()
        st, _ = self.quiet(chain.run_parent, self.c, self.job(jid))
        self.assertEqual(st, "waiting")
        self.assertEqual(self.job(jid)["status"], "waiting_limit")
        self.assertEqual(self.job(f"{jid}-build")["status"], "waiting_limit")
        self.assertEqual(len(limitfb.pending_notices(self.c)), 1)
        self.c.execute("UPDATE jobs SET wait_until=? WHERE id=?", (time.time() - 1, f"{jid}-build"))
        self.c.execute("UPDATE seats SET cooldown_until=? WHERE id='codex-sub'", (time.time() - 1,)); self.c.commit()
        self.quiet(worker.cycle, self.c, self.cfg, lambda ids: [runner.cmd_run(argparse.Namespace(job_id=i)) for i in ids])
        self.assertEqual(self.job(jid)["status"], "done")

    def test_chain_two_seats_limit_moves_step(self):
        self.enable("codex-sub", "claude-strong")
        self.fake.behavior = {"claude-strong": ["limit", "ok"], "codex-sub": ["ok"]}
        jid, _ = self.new_chain()
        st, _ = self.quiet(chain.run_parent, self.c, self.job(jid))
        self.assertEqual(st, "done")
        plan, review = self.job(f"{jid}-plan"), self.job(f"{jid}-review")
        self.assertEqual(plan["seat_id"], "codex-sub"); self.assertTrue(plan["moves"])
        self.assertEqual(review["seat_id"], "codex-sub"); self.assertIn("cooling down", json.loads(review["moves"])[0]["why"])

    def test_parse_reset(self):
        t = 1_800_000_000
        self.assertEqual(limitfb.parse_reset("try again in 2 days 3 hours", t)[0], t + 2 * 86400 + 3 * 3600)
        self.assertEqual(limitfb.parse_reset("Claude AI usage limit reached|1800009999", t)[0], 1800009999)
        self.assertEqual(limitfb.parse_reset("Retry-After: 120", t)[0], t + 120)
        r, how = limitfb.parse_reset("5-hour limit reached - resets 3pm", t)
        self.assertTrue(t < r <= t + 86400 and time.localtime(r).tm_hour == 15, how)
        self.assertEqual(limitfb.parse_reset("nothing here", t), (None, None))
        self.assertTrue(core.LIMIT_RE.search("You've hit your usage limit"))


class TestSuggest(Base):
    def test_only_connected_seats(self):
        self.enable("codex-sub")  # research prefers Claude, but only Codex is connected
        r = suggest.suggest(self.c, self.cfg, "Do an in-depth research report comparing the top 10 competitors in the EU e-bike market with sources")
        self.assertTrue(r["heavy"]); self.assertEqual(r["handler"], "seat")
        self.assertEqual(r["seat"], "codex-sub"); self.assertEqual(r["provider"], "codex")
        self.assertIn("Run it?", r["offer"]); self.assertNotIn("Claude", r["offer"])
        self.enable("codex-sub", "claude-strong")
        r = suggest.suggest(self.c, self.cfg, "Do an in-depth research report comparing the top 10 competitors in the EU e-bike market with sources")
        self.assertEqual(r["seat"], "claude-strong")
        self.enable("claude-strong")
        r = suggest.suggest(self.c, self.cfg, "Build a full web app with login, dashboard and a REST API")
        self.assertEqual(r["seat"], "claude-strong"); self.assertTrue(r["use_chain"])

    def test_no_seat_and_light(self):
        self.enable()
        r = suggest.suggest(self.c, self.cfg, "Build a full web app with login and dashboard")
        self.assertEqual(r["handler"], "grok"); self.assertIsNone(r["model"])
        self.enable("codex-sub")
        self.assertEqual(suggest.suggest(self.c, self.cfg, "what is the capital of Peru?")["handler"], "grok")
        self.assertEqual(suggest.suggest(self.c, self.cfg, "translate this sentence to German")["handler"], "grok")

    def test_declined_not_offered_again(self):
        self.enable("codex-sub")
        text = "Build a full web app with login, dashboard and a REST API"
        self.quiet(suggest.cmd_suggest, argparse.Namespace(text=[text], declined=True, json=False))
        r = suggest.suggest(self.c, self.cfg, "build a complete scraper tool for product prices")
        self.assertTrue(r["declined_before"]); self.assertIsNone(r["offer"])
        self.assertIsNotNone(suggest.suggest(self.c, self.cfg, "clean up this 5000 rows csv dataset and dedupe it fully")["offer"])


class TestMigration(Base):
    def test_old_schema_gets_waiting_limit(self):
        self.c.close(); (TMP / "jevopus.db").unlink()
        old = sqlite3.connect(TMP / "jevopus.db")
        st = ("queued", "leased", "running", "done", "failed", "needs_review", "blocked", "cached", "needs_model")
        old.execute(f"CREATE TABLE jobs(id TEXT PRIMARY KEY, goal TEXT, constraints TEXT, done_when TEXT, kind TEXT, seat_id TEXT, "
                    f"status TEXT CHECK(status IN {st}), lease_until REAL, created_at TEXT, result_path TEXT, route TEXT, tier TEXT, route_note TEXT)")
        old.execute("INSERT INTO jobs(id,goal,status,created_at) VALUES('old1','old goal','done','2026-09-01T00:00:00+00:00')"); old.commit(); old.close()
        c = core.db()
        self.assertEqual(c.execute("SELECT goal, status FROM jobs WHERE id='old1'").fetchone()[:], ("old goal", "done"))
        c.execute("UPDATE jobs SET status='waiting_limit' WHERE id='old1'"); c.commit()
        cols = {r[1] for r in c.execute("PRAGMA table_info(jobs)")}
        self.assertTrue({"chain", "chain_id", "wait_until", "limit_notice", "moves"} <= cols)
        self.assertEqual(c.execute("SELECT v FROM meta WHERE k='schema_rev'").fetchone()[0], "v2.7")


class TestDocs(unittest.TestCase):
    def test_no_reuse_claim(self):
        from jevopuslib import guide
        texts = [Path(guide.__file__).read_text(), (ROOT / "README.md").read_text()]
        texts += [p.read_text() for p in (ROOT / "skills").glob("*/SKILL.md")]
        for t in texts: self.assertNotIn("reuses old results", t.lower())


if __name__ == "__main__":
    unittest.main()
