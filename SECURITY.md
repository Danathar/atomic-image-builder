# Security Policy

Atomic Image Builder authenticates to GitHub on your behalf (`gh auth login`
or a forwarded `GH_TOKEN`), creates and pushes to repositories it manages, and
can rotate a cosign signing key used to sign built images. That is real
credential- and supply-chain-adjacent surface area. If you find a
vulnerability, please do not open a public issue for it -- report it
privately instead, using the process below.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository:
[Report a vulnerability](https://github.com/Danathar/atomic-image-builder/security/advisories/new).
That opens a private advisory visible only to you and the maintainer, and
supports an ongoing discussion and coordinated disclosure once a fix exists.

Please include, where relevant:

- The version (`aib-tool --version`) or commit you tested against
- Steps to reproduce, or a proof of concept
- What you think the impact is — token exposure, arbitrary code execution in
  a generated repo or workflow, signing-key mishandling, and similar

## Scope

In scope: this tool's own code (`atomic_image_builder.py` and the maintainer
scripts), the bundled template snapshots it generates repos from, and the
workflows/Containerfiles it ships or writes into a generated repo.

Out of scope: the upstream projects it builds on top of
([BlueBuild](https://github.com/blue-build/cli),
[rpm-ostree](https://github.com/coreos/rpm-ostree),
[bootc](https://github.com/bootc-dev/bootc), GitHub itself) — please report
those to their own maintainers.

## Working on this with a coding agent

Much of the maintenance here is done with coding agents, which adds a surface
this file does not otherwise cover: prompt injection through the untrusted
input the tool parses, and the fact that `template_snapshots/` and the action
pin tables are copied into every repository the tool generates.
[docs/SECURITY-AI.md](docs/SECURITY-AI.md) covers that specifically. An
AI-specific security problem is a vulnerability in this project and goes
through the private channel above, not a public issue.

## What to expect

This is a small project maintained in the open, without a dedicated security
team or an SLA. You should get an initial response within a few days. Please
give a reasonable amount of time to investigate and ship a fix before any
public disclosure.

## What this is not

This project is not affiliated with or sanctioned by Universal Blue, Fedora,
or GitHub — see the disclaimer in [README.md](README.md). Vulnerabilities in
those projects are theirs to receive, not this repository's.
