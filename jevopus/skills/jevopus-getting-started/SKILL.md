---
name: jevopus-getting-started
description: >-
  Use on a new owner's first conversation with the Jevopus bot, when the user
  asks what Jevopus can do or how to set it up, or when they ask how to work
  with it: silently read their whole Grok Bot context, map their hardest real
  tasks to Grok, Claude, ChatGPT Codex or Jev, walk through setup step by step
  with a test after each step, then explain fully how to use it.
---
# Jevopus: first conversation and guided setup

Reply in the user's language. Keep each message short; one step at a time. Never invent facts about the owner: every suggestion must point to a real signal you found.

## 1. Read the owner's context first (silently, before the first message)
Collect only what is already available to you; don't ask the owner questions yet.
- Teammates: every other bot in your context. Note each bot's name, description and what kind of work it does (code, writing, research, outreach, finance, support...).
- Their skills: the skill catalog (names and descriptions) shows what workflows the owner already runs.
- Routines: your own and, where visible, teammates' scheduled or event jobs. Recurring = good candidate for a seat.
- Connectors: GetMcpServerStatus (GitHub, Gmail, X, Slack, Google Drive/Sheets, Notion, Linear, finance...). Connected service = real data the seats can work on.
- Memory: RecallMemory scope "user" (and "all") for role, company, projects, language, audience, recurring pains, deadlines.
- Computer: `/workspace` folders, repos or data files other bots left there (names only; don't open private files without reason). Check `/workspace/jevopus/jevopus.py`; if it exists run `python3 /workspace/jevopus/jevopus.py doctor`.
- Do not read other bots' full transcripts unless the owner agrees.

## 2. Build the owner's task map
From the signals, list 5 to 7 concrete, heavy tasks this owner plausibly has (long, multi-step, or quality-critical; not quick chat). For each decide who does it:
- **Grok itself (no seat):** quick answers, short edits, lookups, anything done in one or two steps. Say so honestly: most daily work doesn't need a seat.
- **Claude Code (Opus/Sonnet/Haiku):** long careful writing, deep analysis and reasoning, reviewing and refactoring existing code, long documents, strategy.
- **ChatGPT Codex (GPT):** building code, scripts and apps from scratch, data processing, spreadsheets and file conversions, automation.
- **Jev picks:** when both fit, when cost matters, or for recurring jobs where past results should decide. Jev chooses the model and effort per job and learns from the owner's good/wrong feedback.
Claude and ChatGPT are equal options; the split above is a starting heuristic, not a rule. Mention "best of two" for quality-critical jobs (both models run, the better result wins).

## 3. First message
1. One short paragraph: Jevopus runs big jobs on ChatGPT Codex and Claude Code seats (the owner's existing subscriptions, not API billing) on its own computer, 24/7. Grok handles the easy work; only the tasks where a stronger model clearly helps go to a seat, and Jev picks which one. It checks results and stops before sending, paying or deleting.
2. The task map as a short list: task, who does it (Grok / Claude / ChatGPT / Jev), why in a few words, and the signal it came from ("your X API bot builds apps", "GitHub is connected", "memory says you write weekly reports").
3. If you found almost nothing, say so and offer the generic recipes instead: research brief, landing page, code review, data cleanup, X post drafts, short video, lead finder with pitch drafts, media kit, competitor analysis, presentation.
4. End with: setup takes about 5 minutes, then I can start with task #1. Start now?

## 4. Setup, one step per message, test after each
1. Install: run jevopus-install (background). Report doctor's result in plain words.
2. Models: ask which they have (widget, multi-select): ChatGPT subscription, OpenAI API key, Claude (Claude Code). Set up each chosen seat per jevopus-operate:
   - ChatGPT: `setup-seat codex-sub`, send the URL and one-time code; they confirm in their own browser.
   - OpenAI API key: warn first that API usage is billed separately from ChatGPT and needs a positive balance; secret-request `OPENAI_API_KEY`, then `setup-seat codex-api`.
   - Claude Code: install the CLI if needed, then `setup-seat claude-strong` and walk them through the Claude login. The seat offers Opus, Sonnet and Haiku; Jev picks among them per job.
   Each seat is enabled only after its live test passes; tell them what passed. If they only have one of the two, adjust the task map: tasks marked for the missing model go to the connected one or stay with Grok, and say so.
3. Jev: secret-request `TYPESAFE_API_KEY` (key from their TypeSafe account). Test with doctor's "live Jev call" line. Without it Jevopus still works on keyword rules.
4. If a saved key is not visible to doctor: keys reach the computer only after [Update Grok Bot's Computer](grokbot://app/v1/settings?id=update-computer). Send the link, wait, re-run doctor. Tools in `~/.local` usually survive; re-run jevopus-install if doctor says a CLI is missing.

## 5. Explain how to work with you (also whenever they ask "how do I work with you?")
Run `python3 /workspace/jevopus/jevopus.py guide` and explain all of it in the owner's language. Don't paste the English text: translate it naturally and keep every point: plain-word tasks, naming a model and what happens if it isn't connected, best of two, the recipe list, limits, reports with the exact prompt and log, good/wrong feedback teaching model choice, other bots via JEVOPUS JOB, their yes for anything irreversible, weekly updates only with their yes, Jev shadow/active mode. Include the "Now:" line about which seats and models are connected. Remind them that other bots from the task map can hand jobs over with a JEVOPUS JOB message.

## 6. First real job and optional tests (only if the owner agrees)
Offer to run task #1 from the map as the first real job. Optional checks, each using a little seat allowance except the first: (a) `jevopus.py limits`; (b) a research brief with live web search on a short topic, then check it cites real, current URLs; (c) with two models connected, a best-of-two test on a small task (`--best-of-two`), then `report` to show both results and the winner. Run only what they accept and report tokens and results after each.

## 7. Wrap up
Offer a weekly routine that checks for Jevopus updates (see jevopus-operate) and, if it fits, turn a recurring task from the map into a routine that hands the job to a seat. Create routines only if they agree.
Never ask for a pasted key, never print secrets, never run sends, payments or deletions without their explicit yes.
