# agy-delegate

A Claude Code skill for using [Google Antigravity CLI](https://antigravity.google) (`agy`) as a helper: a **quick read-only ask** (second opinion, review, brainstorm) or a big, self-contained coding job where Claude acts only as **monitor and gate**. Claude picks the run mode itself: a single agent (Hydrogen, Neon or an Orchestrator model) or an agy `/swarm` of parallel workers plus a verifier for jobs that split into independent parts. agy works in an isolated git worktree on branch `agy/<job>`, with per-job permission rules and a fence hook. Claude re-runs the acceptance checks itself, reviews the diff, and accepts the result or sends rework into the same agy conversation.

## Install
```sh
# personal (all projects)
cp -r agy-delegate ~/.claude/skills/agy-delegate
# or per project
cp -r agy-delegate <project>/.claude/skills/agy-delegate
```
Claude Code discovers it via `SKILL.md`. Nothing else needs installing.

## Requirements
- Python 3.8+ (stdlib only), `git` 2.20+ (worktrees)
- `agy` on `PATH` (or set `AGY_BIN`), already logged in. Tested with agy 1.5.16 on Linux with a consumer account.
- Linux or macOS. The terminal sandbox needs unprivileged user namespaces on Linux (`unshare -Urn true` must work).
- Optional: user-level systemd (`systemd-run --user`). With it, the detached agy run survives agent harnesses that kill child processes when a tool call returns. Without it, the script falls back to `setsid`.

## Quick ask
```sh
S=~/.claude/skills/agy-delegate/scripts/agy_job.py
python3 $S ask --prompt "SQLite or JSON files for a small CLI todo app? Verdict first, then 3 reasons."
python3 $S ask --prompt-file /tmp/review.md --context-dir ./src --json   # agy may read ./src, never write it
```
One `agy -p` run in a throwaway workspace with a timeout (default 600 s, model `gemini-4-neon-high`). Context dirs are added read-only (settings deny + fence hook + sandbox) and checked for changes afterwards. Prints the answer (or JSON with `state`, `conversation_id`, `tokens`, `blocked`, `context_changed`); `--conversation <id>` asks a follow-up.

## Quick example (gated job)
```sh
S=~/.claude/skills/agy-delegate/scripts/agy_job.py
cd my-repo
cp ~/.claude/skills/agy-delegate/templates/job-spec.md /tmp/roman-spec.md   # fill it in
python3 $S recommend --spec /tmp/roman-spec.md   # -> e.g. --mode single --model gemini-4-hydrogen-high --budget 20
python3 $S start  --job roman --spec /tmp/roman-spec.md \
                  --accept "python3 -m unittest discover -s tests" --allow roman.py --protect 'tests/*'
                  # --mode auto (default) applies the recommendation; or pass --mode single|swarm --model ... --budget ...
python3 $S status --job roman --wait 20     # cheap; blocks until done/stuck/timeout or 20 min
python3 $S gate   --job roman               # exit 0 pass / 1 fail / 2 scope violation / 3 not ready
python3 $S diff   --job roman               # review everything, incl. new files
python3 $S rework --job roman --feedback "from_roman must reject 'IIII'"   # same conversation
python3 $S accept --job roman -m "Add roman numeral converter"            # commit on agy/roman, no push
python3 $S cleanup --job roman              # remove worktree, keep branch
```
Other commands: `ask`, `list`, `stop`. Every command accepts `--json`.

## Where things go
| What | Where |
|---|---|
| Job state, logs, per-job settings, gate results | `<git-common-dir>/agy-jobs/<job>/` (inside `.git`, never committed) |
| Worktree | `$AGY_DELEGATE_WORKTREES` or `${XDG_STATE_HOME:-~/.local/state}/agy-delegate/worktrees/<repo>-<hash>/<job>` |
| Fence hook | `<worktree>/.agents/hooks.json` (pointing at `scripts/fence_hook.py`); left out of commits |
| Agent scratch | `<worktree>/PROGRESS.md`; left out of commits |

Global agy settings are never modified. Per-job rules are passed with `agy --settings <file>`.

## Modes
| `--mode` | What runs | When |
|---|---|---|
| `single` | one agy agent; any model incl. `gemini-pro-orchestrator[-neon\|-argon]` | default for most jobs |
| `swarm` | agy `/swarm` (preview): a coordinator, one worker per spec Part, a verifier and a stall monitor, all in the job worktree | 3+ independent Parts, or 2 big ones; uses several times the quota |
| `auto` (default) | whatever `recommend` picks from the spec (Parts, Scope size, hard/vague keywords) | |

`SKILL.md` has the decision rubric. For swarm jobs, `status` lists each worker's state, model, tool calls (from the fence hook log) and last board post.

## Environment variables
`AGY_BIN` (agy path), `AGY_DELEGATE_WORKTREES` (worktree root), `AGY_DELEGATE_LAUNCHER` (`systemd` or `setsid`), `AGY_DELEGATE_GRACE_S` (seconds past the budget before the runner kills agy; default 120), `AGY_DELEGATE_HOOK_DEBUG=1` (the fence hook logs allowed calls as well as denials).

## Tests
```sh
python3 -m unittest discover -s tests -v    # uses tests/fake_agy.py, no quota used
```
`tests/e2e.md` records the real end-to-end runs against agy.

## Caveats
- **Terms of service:** whether automated or third-party agents may drive `agy` on a consumer Google account is not clearly covered by public docs. Check the Antigravity / Google terms for your account type before using this at scale.
- See "Safety model" in `SKILL.md`. In headless mode on consumer accounts, sandbox-bypass requests were auto-approved, and only the deny rules plus the fence hook stop them. Treat this as defense in depth, not a hard security boundary. Use it only on repos you'd let an agent touch.
- agy prints `--mode auto is not available for this account` in print mode, but on the tested account that warning is wrong (it is printed before silent auth finishes): `-p` runs really are in auto mode, as agy's own log shows (`jetski_mode_auto`). The script pins `--mode auto` and `status` shows the effective mode. Auto mode's sandbox-bypass and outside-workspace-write gaps are closed by the fence hook (see `SKILL.md`).
- Swarm is a preview feature. Only the coordinator's token usage appears in agy's result; workers use quota on top of that.
