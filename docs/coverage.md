# What is coverage?

The **Unit coverage** badge in the [README](../README.md) is [code coverage](https://en.wikipedia.org/wiki/Code_coverage): the share of this project's own source lines — and of the branches through them — that the automated test suite actually executes when it runs. CI measures it with [coverage.py](https://coverage.readthedocs.io/) on every push and pull request, and fails the build if it drops below 90%.

Read it as a floor, not a grade. It says how much of the code is exercised by tests at all; it does not say the tested parts are correct, since a line can be run by a test that never checks the result. What it does tell you is where nothing is watching: uncovered code has never been run by a test, so no test would notice if a change broke it. That matters here because this tool creates and edits GitHub repositories on your behalf, and most of what it does is impractical to verify by hand on every change.

The badge links to the [full history](https://raw.githubusercontent.com/Danathar/atomic-image-builder/coverage-data/coverage-trend.csv), one row per push to `main`, so the trend is visible rather than just today's snapshot. Unit coverage is only one of four separate measurements this project tracks, and they are not interchangeable — [CONTRIBUTING.md](../CONTRIBUTING.md#coverage) explains what each one covers and how to reproduce it locally.
