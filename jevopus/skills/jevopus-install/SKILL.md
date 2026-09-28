---
name: jevopus-install
description: >-
  Use when Jevopus (the Codex and Claude Code worker farm with Jev routing) is
  not yet installed on this bot's computer, `jevopus.py doctor` fails, or the
  user asks to install, reinstall, update or repair it.
---
# Jevopus install / update

Code lives in the public repo `https://github.com/Bodila51/grok-bot-jev` (folder `jevopus/`). The same clone is also the Jev router. Live install goes to `JEVOPUS_HOME` (default `/workspace/jevopus`).

## Check first
1. `export PATH=$HOME/.local/bin:$PATH`
2. If `/workspace/jevopus/jevopus.py` exists, run `python3 /workspace/jevopus/jevopus.py doctor`. If it is all green apart from seat logins, skip to setup (jevopus-operate / jevopus-getting-started).

## Install or update
Run in the background (block_until_ms 0); it takes 10 s to a few minutes:
```
cd /workspace && (test -d grok-bot-jev && git -C grok-bot-jev pull --ff-only || git clone https://github.com/Bodila51/grok-bot-jev) \
  && bash grok-bot-jev/jevopus/install.sh
```
- Add `--no-render` to skip the optional video-render venv (Pillow/numpy/pycairo) when the user doesn't need video recipes.
- The installer installs Node 22 and the Codex CLI into `~/.local` if missing, builds the Jev venv and Jevopus, creates `jevopus.db` with every seat disabled, and ends with `doctor`. For Claude Code it prints the `npm install` command; run it if the owner wants Claude. Re-running is safe; the database and jobs are kept.
- If `apt update` hangs (third-party sources such as Google Chrome), the installer retries with a timeout and only Debian/NodeSource sources. Do not remove the user's apt sources.

## After install
- Read the doctor output. Expected on a fresh install: "at least one enabled seat" is red and seat logins are yellow. Everything else should be green.
- `TYPESAFE_API_KEY` missing means Jev falls back to keyword rules. Ask for it with a secret-request (name `TYPESAFE_API_KEY`); never ask for a pasted key.
- A key the user saved that doctor still doesn't see: new keys only reach the computer after [Update Grok Bot's Computer](grokbot://app/v1/settings?id=update-computer). Send that link, then re-run doctor.
- Then enable seats (see jevopus-operate).

## Upgrades
Upgrading keeps data: new config settings (`limits`, `best_of_two`) and new job columns are added automatically, and edited values are kept. Back up `config.json` and `jevopus.db` first anyway. New installs start in Jev shadow mode.

## Rules
Never print, echo or log secret values. Workers never get credentials.
