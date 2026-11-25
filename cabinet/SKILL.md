---
name: cabinet
description: Convene a small "cabinet" of independent advisers (a Claude subagent plus Google Antigravity `agy`/Gemini) on one question, keep them blind to each other, then reconcile their views and make the call yourself. Use for hard decisions, design or architecture trade-offs, brainstorming, conflicting evidence, high-stakes reviews, or when the user says "ask the cabinet", "brainstorm this", "get second opinions", "what would others think". Not for questions with a known or easily checked answer.
---

# Cabinet

You are the **chair and secretary**. Members give independent views; you frame the question, keep them blind to each other, judge, and own the final call. A cabinet costs time and quota (one strong Claude run plus one agy run, roughly 1–5 minutes), so convene one only when a second mind could change the decision.

## 1. Frame one neutral question
Write `/tmp/cabinet-q.md` (outside any repo) that a stranger could answer cold:
- **Question**: one decision or problem, stated neutrally. Name every serious option; don't reveal which one you or the user lean towards, and don't use loaded words ("obviously", "the simple way").
- **Context**: the facts members need (requirements, scale, constraints, what was already tried, relevant file paths). Members see nothing of your conversation.
- **Answer shape**: "Verdict first (one line). Then up to 5 reasons with evidence, the strongest counter-argument to your own verdict, risks, and what would change your mind. Max ~300 words."

**Done when:** the file contains the question, the context and the answer shape, with no hint of a preferred answer.

## 2. Pick the panel
Default: **two members from different model families**, so they don't share blind spots:
- **Claude**: one subagent on the strongest model available (Opus or better). In Claude Code use the Task/Agent tool with that model and the question file's text as the whole prompt; from a shell, `claude -p "$(cat /tmp/cabinet-q.md)" --model opus`.
- **agy**: `python3 <agy-delegate>/scripts/agy_job.py ask --prompt-file /tmp/cabinet-q.md --json` with `--model gemini-4-neon-high` (default) or `--model gemini-4-carbon-medium` for the hardest questions. Add `--context-dir <dir>` when members must read code. Needs the `agy-delegate` skill.

Add a third member (e.g. a second Claude subagent briefed as devil's advocate, or another Gemini model) only when the stakes are high or the first two are likely to agree for the same reason. Never more than 3.

**Done when:** you know each member, its model, and its read-only context.

## 3. Run members in parallel, blind
Launch all members at the same time with the **same** question file. No member sees another's answer, your own draft, or the user's preference. Members must not edit files (agy `ask` enforces this; tell a Claude subagent "read-only, do not modify files").

**Done when:** every member has returned an answer, or failed once and been noted as absent.

## 4. Optional second round (only for real disagreement)
If members disagree on the verdict or on a fact that decides it, send each one the **other side's strongest argument, unattributed** ("Another reviewer argues: ...") and ask whether it changes their view and why. Use agy `ask --conversation <id>` / a follow-up to the same subagent. One round at most. Skip it when they already agree or differ only in wording.

## 5. Reconcile and decide (you, not the members)
- Check the facts members rely on (run the command, read the file, test the claim) before weighing them. A confident tone or mutual agreement is not evidence.
- **Don't average.** Judge which reasoning is better, and say why. A minority view with better evidence wins.
- Attribute every point: "Claude (Opus): ...", "agy (Neon high): ...".
- Write the report with `templates/report.md`: question, panel, agreements, disagreements with each side's reasoning, your decision and why, open questions / what would change it. `examples/sqlite-vs-json.md` is a real filled-in report.

**Done when:** the report names a decision (or a concrete next experiment if the evidence is truly insufficient) and every claim in it is attributed or verified.

## Rules
- Members stay blind to each other in round 1; the chair never leaks a preferred answer.
- The chair owns the final call; members advise. Never forward a member's answer as the decision.
- Cite who said what; mark what you verified yourself.
- Keep it cheap: two members, one round, by default. Mention the quota cost if the user asked for a large panel.
