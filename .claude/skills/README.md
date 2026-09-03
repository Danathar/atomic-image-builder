# Skills

Packaged procedures for Claude Code in this repo. Versioned deliberately:
`.gitignore` excludes the rest of `.claude/` as personal config, and
re-includes this directory so what is shared stays shared.

Start with [`.github/copilot-instructions.md`](../../.github/copilot-instructions.md).
It is the canonical brief for working here, and these skills assume it.

| Skill | Use |
|---|---|
| [`verify-change`](verify-change/SKILL.md) | Run the full local gate before pushing |

A skill belongs here when the steps are exact, ordered, and easy to get subtly
wrong. Guidance that is merely worth knowing belongs in the canonical brief
instead, and a task that needs framing rather than a procedure belongs in
[`.github/prompts/`](../../.github/prompts/).
