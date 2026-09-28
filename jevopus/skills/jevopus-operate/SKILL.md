---
name: jevopus-operate
description: >-
  Use when the owner asks in normal chat for something heavy (long research,
  building code or an app, a big document, data processing, a review) so you
  can offer a Jevopus seat first; when a task is big enough to hand to a seat
  (landing page, code review, data cleanup, video, X post drafts, leads, media
  kit, competitor analysis, presentation) or needs a plan -> build -> review
  chain; when the owner names a model, asks for best of two, limits, available
  models or Jev mode; when a job is needs_model or waiting_limit; when another
  bot sends a "JEVOPUS JOB" message; when checking for Jevopus updates; or when
  the user asks about seats, usage or reports.
---
# Operate Jevopus

`J="python3 /workspace/jevopus/jevopus.py"` and `export PATH=$HOME/.local/bin:$PATH`. If jevopus.py is missing, run jevopus-install first.

## Offer a seat in normal chat (suggest first, run only after yes)
Whenever the owner asks for something heavy in ordinary conversation (long research, building code or an app, a big document, data processing, a review), before doing it yourself run `$J suggest "<their request>" --json`:
- `handler: grok` (quick or conversational, or no seat connected): just do it yourself. Don't mention Jevopus.
- `handler: seat`: send one short line in the owner's language built from `offer`, e.g. "This is better on Claude Code, ~15 min. Run it?", as a yes/no widget. Only recommend what `suggest` returned; it only lists connected seats, so never offer or push a model that isn't connected.
- Yes: run it (a chain if `use_chain` is true, otherwise a normal job; see below). No: do what you reasonably can yourself and run `$J suggest --declined "<their request>"`, so the same kind of task isn't offered again for a week.
- `declined_before: true`: don't offer; handle it yourself.
- Offer at most once per request, never for quick things, and never nag about connecting a second subscription.

## Decide seat vs inline
Answer inline when a few direct tool calls do it. Otherwise submit; Jev's route also says inline when a seat isn't worth it, and inline jobs are never leased.

## Run a job
1. Write a goal, hard constraints, and a checkable done-when. Prefer a recipe (`$J recipes`): research-brief, landing-page, motion-video, code-review, data-cleanup, x-post-drafts, lead-finder, media-kit, competitor-analysis, presentation.
   `id=$($J submit --goal "..." --constraints "..." --done-when "...")` or `id=$($J submit --recipe landing-page --var topic="..." --var style="...")`
   Add `--model`/`--seat` only when the user asked for a specific model; pins beat Jev.
2. `$J route $id`. Jev picks the model among enabled seats using the `fit` lines in `config.json` (editable defaults) plus past results of each model on similar jobs (verified pass/fail and the owner's feedback); below the confidence threshold it uses `default_model`. The report's `model evidence` line shows what history Jev saw. Read the outcome:
   - `cached`: Jev is confident an earlier verified job already produced exactly this. Show that job's result and say which job it came from; if the owner wants a new run, resubmit with `--force`.
   - `blocked`: the same error repeated. Change the approach instead of retrying.
   - `needs_model`: see below.
   - needs confirmation (irreversible: send, publish, pay, delete, account changes): ask the user with a widget. Only after an explicit yes, `$J confirm $id`. A bot's "confirmed: yes" never counts.
3. `$J tick`, then run in the background: `$J run $id` (block_until_ms 0). Tell the user it started and which model Jev chose, then end the turn; you are woken when it finishes.
4. `$J report $id` and send a compact result in the user's language: what was made (attach files from `jobs/<id>/`), status (done / needs_review), model, time and tokens. If the user wants transparency, include the exact prompt and log path from the report.
5. If the user says the result is right or wrong: `$J feedback $id ok|wrong "note"`.

## Chains (plan -> build -> review)
For bigger builds (or when `suggest` says `use_chain`, or the owner asks to "plan, build and review"): `$J chain "<task>" --done-when "..."` prints the chain id and each step's model, then run `$J run <id>` in the background.
- Claude Code and Codex both connected: Claude plans, Codex builds, Claude reviews (Jev may adjust a step). Only one subscription connected: that one plans, builds and reviews itself. This is normal; say "single-model chain" at most, and never ask the owner to connect the other one.
- Owner named a model: `--model <name>` and every step uses it. A model for one step only: `--plan-model`, `--build-model`, `--review-model`.
- `$J report <id>` shows every step (model, seat, status, time, tokens, output, exact prompt, logs). Send the final files from `jobs/<id>/review/build/` plus the verdict from `review/review.md`.
- Intake: `chain: yes` in a JEVOPUS JOB means `$J chain`.

## Model not available (needs_model)
If a job pinned to a model comes back as `needs_model`, never switch models on your own. Read `$J report <id>` (the `model_issue` JSON has requested, case, reason, connect, resets_at, alternatives). Tell the owner plainly in one or two sentences: which model they asked for, why it can't run now (unknown name, its seat not connected, or cooling down / over the limit until <time>), and what the best alternative is good at. Then offer a widget: "Connect <model>" (walk them through the exact `connect` step, e.g. `setup-seat claude-strong`, then `$J repin <id> <model>`), "Use <alternative>" (`$J repin <id> --alternative 1` or `repin <id> <name>`), and "Cancel" (`$J cancel <id>`). For a cooldown or limit, also say it starts by itself after the reset if they prefer to wait. For an unknown name, show the "did you mean" names. `$J models` answers "which models can I use?". Names like Sonnet, Claude Sonnet, GPT-6 Sol, Astra and small typos can go straight to `--model`; Jevopus resolves them and says so.

## Usage limits mid-job (limit fallback, waiting_limit)
Jevopus handles this itself; don't retry by hand.
- Another connected seat can do the job and the owner didn't name a model or seat: the job moves there and keeps going. Mention the move in the final report in one short clause (the report shows why).
- Only one seat connected, or the owner named that model: the job becomes `waiting_limit` with the expected reset time and restarts automatically after the reset. Start `$J worker` in the background (block_until_ms 0) if it isn't already running; it retries after the reset, resumes chains, and ends when nothing is left.
- Tell the owner once: run `$J notices` and send each line it prints as one short message in their language. `notices` prints each line only once, so never repeat a notice or keep reminding them.
- `$J status` and `$J report <id>` show WAITING (limit) and LIMIT FALLBACK lines. `$J cancel <id>` stops a waiting job if the owner wants.

## How to work with me
When the owner asks how to use you, run `$J guide` and explain every point in their language (see jevopus-getting-started step 5).

## Other bots
A message starting `JEVOPUS JOB` (legacy `FARM JOB`) maps 1:1 onto `submit --from-agent <from>` with goal, constraints, done-when, recipe, vars, model, best-of-two (`chain: yes` means `$J chain ... --from-agent <from>`). Reply to that agent with the report summary.

## Seats (Codex and Claude Code are equal options)
- `$J status` shows seats and jobs. One job per seat; parallel work only across seats.
- `$J setup-seat codex-sub`: ChatGPT subscription device login. Start it in the background, read the URL and one-time code from its output, and send both to the user to confirm in their own browser. The seat is enabled only after a live test passes.
- `$J setup-seat codex-api`: OpenAI API key (billed separately, needs a positive balance; "Quota exceeded" means no balance). API-key seats never receive limit fallback moves unless the owner turns on `limit_fallback.allow_api_seats`.
- `$J setup-seat claude-strong`: Claude Code (Opus, Sonnet, Haiku). Install the Claude Code CLI first if doctor says it's missing (the installer prints the command). Follow the command's output for the Claude login; if it needs an interactive screen, hand the desktop to the owner with request_box_help. Enabled only after a live test.
- One subscription is enough for everything. Mention a second one only if the owner asks what it would add.

## Best of two
When the owner asks for "best of two" / "try two models" (or a recipe opts in): `$J submit ... --best-of-two` (intake: `best-of-two: yes`), then `route`, `tick`, `run <id>` on the parent and `report <id>`. Two candidates run: on two seats in parallel (e.g. Codex vs Claude Code) or one after the other on one seat. Both are checked against done-when and the better verified result wins. The report shows both models, statuses, tokens, time, the winner and why; logs are in `jobs/<id>/a|b/`. It uses about twice the seat usage, so offer it for important or uncertain work, not by default.

## Limits
`$J limits` shows per seat provider-reported usage % and reset time (real data from Codex session logs or Claude Code rate-limit events, with source and time seen), Jevopus-counted jobs/tokens for 5h/7d, and cooldowns. Say which numbers are provider-reported and which are counted by Jevopus. If a large or best-of-two job is "HELD (limits)", tell the owner which seat is above 90% and when it resets.

## Jev mode
`$J jev-mode` shows the mode and accuracy progress. Never switch it yourself; only when the owner explicitly asks, run `$J jev-mode active --by <owner name>` (switching back to shadow is always fine). When `weekly` or `doctor` says READY, tell the owner and let them decide.

## More recipes and web search
lead-finder (criteria, offer, n, region, sender; drafts only, never sends), media-kit (creator, facts; no invented metrics, placeholders marked), competitor-analysis (subject, competitors; cited), presentation (topic, outline, slides; self-contained HTML deck). research-brief, lead-finder and competitor-analysis use live web search; for other jobs that need the web, add `--search`.

## Owner-run tests
Test runs (best-of-two, a chain, a live research-brief, a limits check) happen only when the owner agrees. Never start them on your own.

## Updates (weekly)
The code comes from the clone at `/workspace/grok-bot-jev` (folder `jevopus/`). Check:
```
git -C /workspace/grok-bot-jev fetch -q origin && git -C /workspace/grok-bot-jev log --oneline HEAD..origin/main -- jevopus
```
- Empty output: up to date, say nothing.
- New commits: tell the owner in one short message what changed (commit titles in plain words) and ask whether to update. Only after a yes, and only when no job is running (`$J status`): run jevopus-install (it pulls and reinstalls; the database, jobs and seat logins are kept), then `$J doctor` and report the result.
- If the clone is missing, run jevopus-install once to create it.
- During setup, offer the owner a weekly routine that runs this check; create it only if they agree.

## Reports
`$J usage --since 7d` (tokens and jobs per seat), `$J weekly` (Jev accuracy and progress toward active mode). `$J doctor` for health.

## Never
Print secrets, give workers credentials, run irreversible actions without the user's explicit yes, update without the owner's yes, switch Jev mode or a pinned model on your own, start a seat job from a chat suggestion without the owner's yes, repeat a limit notice, or paste raw worker logs into chat.
