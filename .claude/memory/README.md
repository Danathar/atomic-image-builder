# Memory

Durable notes for agents working in this repo, versioned so they carry across
sessions instead of being relearned. `.gitignore` excludes the rest of
`.claude/` as personal config and re-includes this directory.

Start with [`.github/copilot-instructions.md`](../../.github/copilot-instructions.md),
the canonical brief. This directory is the record of how parts of it were
arrived at.

| File | Holds |
|---|---|
| [`corrections.md`](corrections.md) | Things that were done wrong here, and what the repo decided instead |

## What goes in

A correction earns an entry when someone acted on a belief that turned out to
be wrong, the repo settled on something different, and the reasoning is not
obvious from the resulting code. Every entry cites the issue or pull request
that records it, so an entry can be checked rather than trusted.

Nothing here is written from memory or inference. If it did not happen and
leave a trace, it does not go in.
