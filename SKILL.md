---
name: agy-delegate
description: Outsource a big, self-contained coding job to Google Antigravity CLI (`agy`, Gemini/Orchestrator models, optionally an agy /swarm of parallel workers) and act only as monitor and gate. Use when the user says to delegate or outsource work to agy, Antigravity or Gemini, "let agy do it", or wants a long, well-specified implementation run unattended in an isolated git worktree while you verify the result. Not for small edits you can do faster yourself.
---

# agy-delegate

You are the **gate**, not the worker. agy does the coding in an isolated worktree on branch `agy/<job>`. You write the spec, **choose mode and model yourself**, launch, check status now and then, verify independently, and accept or send rework. Don't do the job yourself and don't babysit the run.

All commands go through `scripts/agy_job.py`. Below, `J` means `python3 <this skill dir>/scripts/agy_job.py`. Run it from inside the target repo, or pass `--repo`. Add `--json` for machine-readable output.

**Ask the user only for missing goals** (what to build, what "done" means). Never ask them about mode, model, budget, scope globs or spec wording: decide those yourself with the rubric below, and say what you picked in the report.

## 1. Write the job spec
Copy `templates/job-spec.md` to a temp file outside the repo and fill in: goal, context, **scope** (the paths agy may change), **out of scope**, **acceptance commands** (objective, runnable, exit 0 = pass), and done criteria. If the acceptance tests don't exist yet, write and commit them first (without `__pycache__`/build junk) and protect them, so agy can't weaken them. When the job splits into independent pieces, fill **Parts**: one bullet per piece with its owner files and the interface it provides. Fix shared interfaces in the spec yourself, so no two parts have to co-design one.
**Done when:** a stranger could do the job from the spec alone, and the acceptance commands fail now.

## 2. Choose mode and model yourself
Run `J recommend --spec /tmp/spec.md`. It reads Parts, Scope and keywords and prints `--mode/--model/--budget` with reasons. `start --mode auto` (the default) applies the same pick. Override it only when you know better:

| Job | Mode | Model |
|---|---|---|
| Small, well specified, clear tests | single | `gemini-4-hydrogen-high` (fast, cheap) |
| Hard but in one area (debugging, races, algorithms, perf) | single | `gemini-4-neon-high` (`-xhigh` if it already failed once) |
| Big (8+ scope paths or 900+ words) or vague (refactor, design, migrate) | single | `gemini-pro-orchestrator-neon` (plans, then executes) |
| Hardest: hard **and** big or vague | single | `gemini-pro-orchestrator` (Carbon) |
| 3+ independent Parts, or 2 Parts in a big job (8+ scope paths or a 900+ word spec) | swarm | `gemini-4-hydrogen-high` workers (`gemini-4-neon-high` if the parts are hard) |

- **Swarm** = agy `/swarm`: a coordinator adds one worker per part plus a verifier and a stall monitor, all in the same worktree. It is faster on wall clock for broad work, but it uses several times the quota of one agent. Use it only when the parts really are independent (separate files, no shared interface to invent). Two small parts are cheaper as a single job.
- Coupled work (one module, cross-cutting change, unclear split) always runs single, however big it is.
- If a single job times out or gets stuck because it's too broad, split it into Parts and rerun as a swarm. If a swarm thrashes (workers editing each other's files, verifier failing in loops), rerun single with an Orchestrator model.

## 3. Start
```sh
J start --job <slug> --spec /tmp/spec.md \
  --accept "python3 -m unittest discover -s tests" \
  --allow 'src/*' --protect 'tests/*'           # mode/model/budget: recommended (or pass --mode/--model/--budget)
```
This creates the worktree and branch from `HEAD` (uncommitted changes are not included; it warns about that), writes per-job permission settings and a fence hook, and launches agy in the background. `start` prints the chosen mode, model, budget and why. State lives in `<git-common-dir>/agy-jobs/<job>/` and is never committed.
**Done when:** `start` prints the worktree and branch with no error.

## 4. Monitor (cheaply)
`J status --job <slug> --wait 20` blocks until the job leaves `running` or 20 minutes pass. Run it as a background command, or re-check every 10–20 minutes. Don't read the raw logs unless something is wrong. For swarm jobs, status also lists each worker: state, model, tool calls, last board post, and whether it posted `[done]`.
States: `running`, `done`, `failed`, `timeout`, `stuck` (no output for `--stuck` minutes), `blocked` (agy needed an approval that headless mode can't give), `stopped`.
- `stuck` or over budget: `J stop`, then `rework` with a narrower ask, or switch mode as in step 2.
- `blocked` or `failed`: read `reason` and `blocked`. Usually the spec needs something the fence forbids (network, installs). Fix the spec, or ask the user.
**Done when:** the state is no longer `running`.

## 5. Gate (independently)
```sh
J gate --job <slug>          # exit 0 pass, 1 acceptance failed, 2 scope violation, 3 not ready
J diff --job <slug>          # full review diff, including new files
```
The gate re-runs every acceptance command in the worktree. It flags changes outside `--allow`, changes to `--protect` or secret paths, tampering with the fence hook, any commits agy made, and extra branches or worktrees that appeared during the job. Then **read the diff yourself** and check it against the spec: scope creep, stubbed or skipped tests, hard-coded answers, dead code, unrequested dependencies, and for swarms, mismatched interfaces between parts. Treat agy's summary, and a swarm verifier's "all green", as a claim to verify, not as evidence. To add a requirement you found while reviewing, use `--add-accept "<cmd>"`.
**Done when:** the gate exits 0 and your diff review finds nothing blocking.

## 6. Accept or rework
- Rework: `J rework --job <slug> --feedback "<concrete, checkable points>"` continues the same agy conversation (for a swarm, the coordinator, which routes each fix to a worker). Then repeat steps 4–5. The default limit is 3 rounds in total (`--max-rounds`). When it's hit, stop and report to the user rather than forcing more rounds.
- Accept: `J accept --job <slug> -m "<commit title>"` commits only the deliverable files on `agy/<job>`. Nothing is merged and nothing is pushed. Add `--merge` only if the user explicitly asked for a local merge. It refuses if the tree changed since the passing gate.
- Then: `J cleanup --job <slug>` removes the worktree and keeps the branch. Add `--delete-branch` and `--purge` only for abandoned jobs.
**Done when:** the branch holds one reviewed commit, or the job is abandoned and cleaned up.

## 7. Report (short)
Give the job, the mode and model **you chose and why** (one line), rounds, gate result (commands and pass/fail), files changed (diffstat), the branch name and how to merge it, anything blocked or left open, and your own review verdict.

Model ids carry the effort level. Don't pass `--effort`, because agy rejects it. `agy models` lists the ids.

## Safety model (read before loosening anything)
- Per-job settings are passed with `--settings` and never written to agy's global settings. `--dangerously-skip-permissions` is never used.
- Headless facts (agy 1.5.16, consumer account, see `tests/e2e.md`): print mode **really runs in auto mode**. The `--mode auto is not available ... rollout flag off` warning is printed before silent auth finishes and is wrong; agy's log then shows `jetski_mode_auto`. The script pins `--mode auto` and `status` reports the effective mode from agy's log. In auto mode, unlisted commands run **inside the terminal sandbox** (no network, `.git` read-only, most of `$HOME` hidden), and an LLM monitor reviews each step. `ask` rules are auto-denied, and `deny` rules return an error while agy carries on. `--agy-mode accept-edits` auto-denies every unlisted command headless, which makes it useless here.
- **Auto mode gaps that the fence closes:** a `BypassSandbox=true` command ran **outside** the sandbox even though the monitor flagged it, and auto mode lets file tools create files outside the workspace or edit other git repos. A command `allow` rule also lets that command leave the sandbox. So the template keeps `allow` empty (only a read-only grant on the git dir). `scripts/fence_hook.py` (installed as `.agents/hooks.json` in the worktree, never committed) denies sandbox bypass, network and remote tools, git push/commit/remote changes, sudo, file writes outside the worktree, reads of identity files (`~/.gitconfig`, `~/.git-credentials`, `~/.netrc`, `~/.npmrc`, `~/.pypirc`, `~/.docker/config.json`, `~/.ssh`, ...), and subagents or swarm workers with their own `share`/`branch` workspace.
- Swarm workers and subagents load the same workspace hook and per-job settings. This was verified live: a worker's `curl` and `git push` were denied by the hook.
- File rules must be absolute; the Linux sandbox rejects relative ones. `agy_job.py` makes them absolute for you. Keep that if you edit `templates/permissions.json`.
- agy cannot commit. You (the gate) own commits, and nothing is ever pushed.
