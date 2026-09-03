# End-to-end tests

These run the **built container image**, not the source tree. Everything in
`tests/` above this directory is a unit or behavioural suite that imports the
module or stubs the commands it calls; nothing there can tell you whether the
image that gets published actually starts.

Two scripts, with different jobs:

| Script                  | Against                         | Asserts                                          |
| ----------------------- | ------------------------------- | ------------------------------------------------ |
| `smoke.sh`              | the plain image                 | behaviour: output, exit status, the command name |
| `coverage_scenarios.sh` | the coverage-instrumented image | only exit status, and writes coverage data       |

The split is deliberate. `coverage_scenarios.sh` is a measurement: it checks
each scenario exited as expected, because one that crashed early would still
write a data file and quietly understate coverage, but the behaviour itself is
`smoke.sh`'s job and is not re-asserted through the coverage shim.

## Running them

Build both images first. `-t aib-local` and `-t aib-local-cov` are the tags the
scripts default to:

```bash
podman build -t aib-local -f Containerfile .
podman build -t aib-local-cov -f container/Containerfile.coverage --build-arg BASE_IMAGE=aib-local .
```

Then:

```bash
tests/e2e/smoke.sh
tests/e2e/coverage_scenarios.sh
python3 -m coverage combine --rcfile=.coveragerc.e2e --keep e2e-coverage/data
python3 -m coverage report --rcfile=.coveragerc.e2e
```

Both take the image tag as their first argument, so CI passes its own
(`aib-ci-test` and `aib-ci-test-cov`) rather than retagging. The coverage
script takes the data directory as its second, defaulting to
`e2e-coverage/data` because that is where `.coveragerc.e2e` and
CONTRIBUTING.md's combine step expect it.

Podman is the only requirement beyond the images; there is no Python
dependency until the `coverage combine` step.

## What is reachable end to end, and what is not

Four scenarios, and that is genuinely all of them: `--version`, its `-V` short
form, `--help`, and the preflight failure. The guided wizard needs a TTY, so
none of it can run here.

That is why the end-to-end coverage number is low and is **not** gated. It
exists so a coverage gap can be classified honestly rather than inferred from
the unit run alone. CONTRIBUTING.md's Coverage section has the full reasoning,
and `.coverage-thresholds.json` records which tiers are gated.

The preflight scenario masks `gum` by bind-mounting an empty, non-executable
file over `/usr/bin/gum`. The tool's `command_exists()` goes through
`shutil.which`, which requires the executable bit, so that is enough to make it
see the binary as absent, and the tool then has to degrade to the plain-text
preflight failure and exit 1 rather than block on a prompt.

Mounts use `:ro,z`. The `z` relabels the source for container access, which an
SELinux host needs and where a bare `ro` mount is denied outright; it is a
no-op where SELinux is off. Since this tool's users are on atomic desktops,
which do enforce SELinux, that is the difference between these scripts running
locally and not.

## Adding a scenario

If it is a new non-interactive path, it belongs in both scripts: an assertion
in `smoke.sh` and a `scenario` line in `coverage_scenarios.sh`, so it is both
proven and measured. A scenario present in only the coverage script gets
counted without anything checking what it did.
