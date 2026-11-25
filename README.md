# skills

Agent skills for daily use with Claude Code (and other agents that read `SKILL.md`).

| Skill | What it does |
|---|---|
| [delegation-routing](delegation-routing/) | Decides how much to delegate (do it yourself → Claude subagent → agy quick ask → agy whole job → cabinet) and to which model, plus briefing and review rules. |
| [agy-delegate](agy-delegate/) | The manual for Google Antigravity CLI (`agy`): a read-only quick `ask` (opinion, review, brainstorm) up to a big coding job in an isolated worktree that Claude monitors and gates. |
| [cabinet](cabinet/) | Brainstorm or decision rule: get independent, blind views from a Claude subagent and agy, then reconcile them and make the call as chair. |

`cabinet` and the agy levels of `delegation-routing` use `agy-delegate`; install all three together.

## Install

Symlink (or copy) skill folders into `~/.claude/skills/` (all projects) or `<project>/.claude/skills/` (one project):

```sh
git clone https://github.com/William-Wang-Dev/skills.git
for s in delegation-routing agy-delegate cabinet; do ln -s "$PWD/skills/$s" ~/.claude/skills/$s; done
```

Each skill's own `README.md` or `SKILL.md` lists its requirements (`agy-delegate`: Python 3.8+, git, a logged-in `agy`).
