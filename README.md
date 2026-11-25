# agy-delegate

A Claude Code skill for handing a big, self-contained coding job to [Google Antigravity CLI](https://antigravity.google) (`agy`) while Claude acts only as **monitor and gate**. agy works in an isolated git worktree on branch `agy/<job>`, with per-job permission rules and a fence hook. Claude re-runs the acceptance checks itself, reviews the diff, and accepts the result or sends rework into the same agy conversation.

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

## Quick example
```sh
S=~/.claude/skills/agy-delegate/scripts/agy_job.py
cd my-repo
cp ~/.claude/skills/agy-delegate/templates/job-spec.md /tmp/roman-spec.md   # fill it in
python3 $S start  --job roman --spec /tmp/roman-spec.md \
                  --accept "python3 -m unittest discover -s tests" --allow roman.py --protect 'tests/*' \
                  --model gemini-4-hydrogen-high --budget 30
python3 $S status --job roman --wait 20     # cheap; blocks until done/stuck/timeout or 20 min
python3 $S gate   --job roman               # exit 0 pass / 1 fail / 2 scope violation / 3 not ready
python3 $S diff   --job roman               # review everything, incl. new files
python3 $S rework --job roman --feedback "from_roman must reject 'IIII'"   # same conversation
python3 $S accept --job roman -m "Add roman numeral converter"            # commit on agy/roman, no push
python3 $S cleanup --job roman              # remove worktree, keep branch
```
Other commands: `list`, `stop`. Every command accepts `--json`.

## Where things go
| What | Where |
|---|---|
| Job state, logs, per-job settings, gate results | `<git-common-dir>/agy-jobs/<job>/` (inside `.git`, never committed) |
| Worktree | `$AGY_DELEGATE_WORKTREES` or `${XDG_STATE_HOME:-~/.local/state}/agy-delegate/worktrees/<repo>-<hash>/<job>` |
| Fence hook | `<worktree>/.agents/hooks.json` (pointing at `scripts/fence_hook.py`); left out of commits |
| Agent scratch | `<worktree>/PROGRESS.md`; left out of commits |

Global agy settings are never modified. Per-job rules are passed with `agy --settings <file>`.

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
- `--mode auto` was not available on the tested account ("rollout flag off"), so jobs run in default mode with the sandbox on.
