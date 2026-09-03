# Prompt catalog

Task prompts for the recurring maintenance work in this repo: the jobs that
come round on a schedule, have a fixed shape, and have a wrong way to do them
that is not obvious.

Each one is a starting point, not a script. They name the procedure and the
trap; the authoritative version of every procedure is in
[maintainer_docs/MAINTAINER.md](../../maintainer_docs/MAINTAINER.md), which
they link to rather than restate.

Before using any of them, read
[.github/copilot-instructions.md](../copilot-instructions.md) — it is the
canonical brief for working in this repo at all.

| Prompt                                                 | When                                                 |
| ------------------------------------------------------ | ---------------------------------------------------- |
| [`triage-weekly-audit`](triage-weekly-audit.prompt.md) | The Monday audit ran and something is in the summary |
| [`refresh-action-pins`](refresh-action-pins.prompt.md) | The audit says a pin trails its tag or branch        |
| [`cut-release`](cut-release.prompt.md)                 | Shipping a new version                               |

One-off work does not belong here. A prompt earns its place by being needed
again.
