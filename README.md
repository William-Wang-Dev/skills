# skills

Agent skills for daily use with Claude Code (and other agents that read `SKILL.md`).

| Skill | What it does |
|---|---|
| [agy-delegate](agy-delegate/) | Outsource a big, self-contained coding job to Google Antigravity CLI (`agy`). Claude picks single or swarm mode and the model itself, monitors in the background, and gates the result (independent tests + diff review) before accepting. |

## Install

Copy or symlink a skill folder into `~/.claude/skills/` (all projects) or `<project>/.claude/skills/` (one project):

```sh
git clone git@github.com:William-Wang-Dev/skills.git
ln -s "$PWD/skills/agy-delegate" ~/.claude/skills/agy-delegate
```

Each skill's own `README.md` lists its requirements.
