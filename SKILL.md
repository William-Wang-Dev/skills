---
name: agy-delegate
description: Outsource a big, self-contained coding job to Google Antigravity CLI (`agy`, Gemini/Orchestrator models) and act only as monitor and gate. Use when the user says to delegate or outsource work to agy, Antigravity or Gemini, "let agy do it", or wants a long, well-specified implementation run unattended in an isolated git worktree while you verify the result. Not for small edits you can do faster yourself.
---

# agy-delegate

You are the **gate**, not the worker. agy does the coding in an isolated worktree on branch `agy/<job>`. You write the spec, launch, check status now and then, verify independently, and accept or send rework. Don't do the job yourself and don't babysit the run.

All commands go through `scripts/agy_job.py`. Below, `J` means `python3 <this skill dir>/scripts/agy_job.py`. Run it from inside the target repo, or pass `--repo`. Add `--json` for machine-readable output.

## 1. Write the job spec
Copy `templates/job-spec.md` to a temp file outside the repo and fill in: goal, context, **scope** (the paths agy may change), **out of scope**, **acceptance commands** (objective, runnable, exit 0 = pass), and done criteria. If the acceptance tests don't exist yet, write or commit them first and protect them, so agy can't weaken them.
**Done when:** a stranger could do the job from the spec alone, and the acceptance commands fail now.

## 2. Start
```sh
J start --job <slug> --spec /tmp/spec.md \
  --accept "python3 -m unittest discover -s tests" \
  --allow 'src/*' --protect 'tests/*' \
  --model gemini-4-hydrogen-high --budget 45
```
This creates the worktree and branch from `HEAD` (uncommitted changes are not included; it warns about that), writes per-job permission settings and a fence hook, and launches agy in the background. State lives in `<git-common-dir>/agy-jobs/<job>/` and is never committed.
**Done when:** `start` prints the worktree and branch with no error.

## 3. Monitor (cheaply)
`J status --job <slug> --wait 20` blocks until the job leaves `running` or 20 minutes pass. Run it as a background command, or re-check every 10–20 minutes. Don't read the raw logs unless something is wrong.
States: `running`, `done`, `failed`, `timeout`, `stuck` (no output for `--stuck` minutes), `blocked` (agy needed an approval that headless mode can't give), `stopped`.
- `stuck` or over budget: `J stop`, then `rework` with a narrower ask, or report to the user.
- `blocked` or `failed`: read `reason` and `blocked`. Usually the spec needs something the fence forbids (network, installs). Fix the spec, or ask the user.
**Done when:** the state is no longer `running`.

## 4. Gate (independently)
```sh
J gate --job <slug>          # exit 0 pass, 1 acceptance failed, 2 scope violation, 3 not ready
J diff --job <slug>          # full review diff, including new files
```
The gate re-runs every acceptance command in the worktree and flags changes outside `--allow`, changes to `--protect` or secret paths, tampering with the fence hook, and any commits agy made. Then **read the diff yourself** and check it against the spec: scope creep, stubbed or skipped tests, hard-coded answers, dead code, unrequested dependencies. Treat agy's summary as a claim to verify, not as evidence. To add a requirement you found while reviewing, use `--add-accept "<cmd>"`.
**Done when:** the gate exits 0 and your diff review finds nothing blocking.

## 5. Accept or rework
- Rework: `J rework --job <slug> --feedback "<concrete, checkable points>"` continues the same agy conversation. Then repeat steps 3–4. The default limit is 3 rounds in total (`--max-rounds`). When it's hit, stop and report to the user rather than forcing more rounds.
- Accept: `J accept --job <slug> -m "<commit title>"` commits only the deliverable files on `agy/<job>`. Nothing is merged and nothing is pushed. Add `--merge` only if the user explicitly asked for a local merge. It refuses if the tree changed since the passing gate.
- Then: `J cleanup --job <slug>` removes the worktree and keeps the branch. Add `--delete-branch` and `--purge` only for abandoned jobs.
**Done when:** the branch holds one reviewed commit, or the job is abandoned and cleaned up.

## 6. Report (short)
Give the job, model, rounds, gate result (commands and pass/fail), files changed (diffstat), the branch name and how to merge it, anything blocked or left open, and your own review verdict.

## Model choice
Effort is part of the model id. Don't pass `--effort`, because agy rejects it. List models with `agy models`.
- `gemini-pro-orchestrator` (planner plus executor; `-neon` or `-argon` variants): big multi-file jobs or vague specs. Slower, uses more quota.
- `gemini-4-hydrogen-high`: the default. Use it for well-specified jobs with clear tests. Fast and cheap.
- `gemini-4-neon-high` (or `-xhigh`): hard algorithmic or debugging work.

## Safety model (read before loosening anything)
- Per-job settings are passed with `--settings` and never written to agy's global settings. `--dangerously-skip-permissions` is never used.
- Headless facts (agy 1.5.x, consumer account): `--mode auto` is not offered, so the run uses default mode. Unlisted commands run **inside the terminal sandbox** (no network, `.git` read-only, most of `$HOME` hidden). `ask` rules are auto-denied. `deny` rules return an error and agy carries on.
- **Sandbox escape:** a command `allow` rule lets that command run outside the sandbox, and `BypassSandbox=true` requests were auto-approved headless. So the template keeps `allow` empty (only a read-only grant on the git dir), and `scripts/fence_hook.py` (installed as `.agents/hooks.json` in the worktree, never committed) denies sandbox bypass, network and remote tools, git push/commit/remote changes, sudo, and file writes outside the worktree.
- File rules must be absolute; the Linux sandbox rejects relative ones. `agy_job.py` makes them absolute for you. Keep that if you edit `templates/permissions.json`.
- agy cannot commit. You (the gate) own commits, and nothing is ever pushed.
