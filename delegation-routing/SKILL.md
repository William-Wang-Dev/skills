---
name: delegation-routing
description: Decide how much of a task to delegate and to whom, before starting any non-trivial work — do it yourself, use a Claude subagent (Haiku/Sonnet/Opus), ask Google Antigravity `agy` for a quick second opinion, hand agy a whole coding job, or convene a cabinet of independent advisers. Covers model choice, briefing, parallelism limits and reviewing results before accepting. Use when a task is multi-step, slow, parallelisable, high-stakes, or would benefit from an independent view, or when the user asks to delegate, outsource, parallelise or "use helpers".
---

# Delegation routing

You are the single owner of the task. Helpers do bounded work; you decide, brief, review, and give the final answer. **Never delegate the final judgment or the reply to the user.**

## 1. Pick the lowest level that fits
| Level | What | Use when | How |
|---|---|---|---|
| **L0** | Do it yourself | Chat, a known answer, a quick lookup, 1–3 tool calls, or anything where briefing costs more than doing | — |
| **L1** | Claude subagent | Multi-step research, codebase search, a separable chunk of work, or parallel independent parts | Task/Agent tool (or `claude -p`) with a model from step 2 |
| **L2** | agy quick ask | You want a second opinion from another model family: review, sanity check, brainstorm, "am I missing something" | `agy-delegate` skill, `ask` (read-only, minutes) |
| **L3** | agy whole job | A long, self-contained, well-specified coding job in one repo with objective acceptance tests | `agy-delegate` skill, `start` → `status` → `gate` → `accept` (single agent or swarm, model rubric there) |
| **L4** | Cabinet | A hard decision, design trade-off, conflicting evidence, or high stakes where independent views should be reconciled | `cabinet` skill (Claude + agy, blind, you chair) |

Go up a level only for a concrete reason (time, parallelism, independence, stakes). Levels combine: e.g. L1 subagents for research, then L4 on the decision.
**Done when:** you can name the level and the reason in one line.

## 2. Pick the model
**Claude subagents (L1):**
- **Haiku**: light, mechanical work such as formatting, grep-and-summarise, simple checks.
- **Sonnet**: normal coding, research write-ups, most subagent work.
- **Opus or the strongest available**: hard reasoning, design, tricky debugging, cabinet member.

**agy (L2–L4)**, model ids carry the effort:
- `gemini-4-hydrogen-high`: fast, cheap detail worker. Best with precise, well-specified instructions. Use it for simple asks and small jobs.
- `gemini-4-neon-high`: default for hard engineering and reviews (debugging, refactors, analysis). Use `-xhigh` when it needs maximum depth.
- `gemini-4-carbon-medium` / `gemini-pro-orchestrator`: hardest only (architecture, security audit, very long or vague multi-step work). Slow and quota-heavy.

**Mix model families.** For a second opinion, a review, or a cabinet, use a different family from the one that did the work. A model reviewing its own family's output shares its blind spots.
**Done when:** every helper has a named model and a reason.

## 3. Brief each helper
Every brief must be self-contained (helpers don't see your conversation) and must state:
1. **Objective**: what to achieve and why. One task per helper.
2. **Output**: the exact shape (verdict first, a table, a diff, a file path). Ask for **checkable evidence**: test output with counts, file:line, source URL with date, before/after numbers.
3. **Write scope**: which paths it may change. Use "read-only" for research and opinions.
4. **Verification**: the command or check that proves it is done.

**Done when:** a stranger could do the task from the brief alone.

## 4. Run
- At most **3 helpers in parallel**, unless the work is clearly independent and cheap (e.g. read-only searches across separate areas).
- Run independent helpers in parallel, and dependent ones in order. Give parallel helpers disjoint write scopes.
- Don't busy-poll. Wait for completion, or check long agy jobs every 10–20 minutes (`status --wait`).
- If a helper fails, retry once with a sharper brief or a stronger model. Then do the work yourself or tell the user.

## 5. Review before accepting
- Run the verification yourself (tests, read the file, a live call). A helper's "done" or "all green" is a claim, not evidence.
- Ignore praise, agreement and confident tone in helper output; they are not evidence.
- Check in proportion to the stakes. Low stakes or easily reversed (summaries, drafts, brainstorms): read it through. Medium (code changes): run the tests and spot-check the diff. High (facts the user will act on, money, config, security, anything sent outside): verify independently, or get a second helper from another family.
- Send fixable gaps back to the same helper (same conversation or session) with concrete points, instead of patching them silently.

**Done when:** each accepted result passed its verification, and you can report which helpers ran, on which models, and anything left unverified.
