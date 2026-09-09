"""Extract one workflow step's `run:` shell and `env:` mapping.

Executing a step's shell is the only way to test it: `run:` bodies are
invisible to every coverage config here, and the dir-wide scans in
tests/test_workflow_dependencies.py read workflows to compare pins, not to
run them. Tests that execute a step extract it from the workflow rather than
copying it, so editing the workflow re-runs the assertions against the edit.

Parsed by indentation instead of with PyYAML: CI installs only `coverage` and
`ruff` for the unit suite (CONTRIBUTING.md pins both), so a test module may
not import a third-party parser. The subset understood here is what these
workflows actually write -- a `- name:` step containing a `run: |` block and
an optional flat `env:` mapping -- and anything outside it raises rather than
returning a half-parsed body.
"""

from pathlib import Path


def _step_lines(workflow_path: Path, step_name: str) -> tuple[list[str], int, int]:
    """The lines of one `- name: <step_name>` step, with its bounds.

    Returns the workflow's lines, the index of the step's `- name:` line, and
    the index one past the step's last line.
    """
    lines = workflow_path.read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}"]
    if len(starts) != 1:
        raise AssertionError(f"expected exactly one {step_name!r} step in {workflow_path}, found {len(starts)}")
    start = starts[0]
    indent = len(lines[start]) - len(lines[start].lstrip())
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped and len(lines[i]) - len(lines[i].lstrip()) <= indent:
            end = i
            break
    return lines, start, end


def step_run_body(workflow_path: Path, step_name: str) -> str:
    """The dedented shell of a step's `run: |` block."""
    lines, start, end = _step_lines(workflow_path, step_name)
    for i in range(start + 1, end):
        if lines[i].strip() != "run: |":
            continue
        body_indent = len(lines[i]) - len(lines[i].lstrip()) + 2
        body = []
        for line in lines[i + 1 : end]:
            if line.strip() and len(line) - len(line.lstrip()) < body_indent:
                break
            body.append(line[body_indent:] if len(line) >= body_indent else "")
        return "\n".join(body).rstrip() + "\n"
    raise AssertionError(f"{step_name!r} in {workflow_path} has no `run: |` block")


def step_env(workflow_path: Path, step_name: str) -> dict[str, str]:
    """A step's `env:` mapping, as written (expressions left unevaluated)."""
    lines, start, end = _step_lines(workflow_path, step_name)
    for i in range(start + 1, end):
        if lines[i].strip() != "env:":
            continue
        env_indent = len(lines[i]) - len(lines[i].lstrip())
        env = {}
        for line in lines[i + 1 : end]:
            if not line.strip():
                continue
            if len(line) - len(line.lstrip()) <= env_indent:
                break
            key, _, value = line.strip().partition(": ")
            env[key] = value
        return env
    return {}
