import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class WorkflowDependencyTests(unittest.TestCase):
    def test_python_ci_tool_installs_are_exactly_pinned(self) -> None:
        expected_commands = {
            ".github/workflows/ai-fix.yml": [
                "pip install coverage==7.16.0 ruff==0.16.5",
            ],
            ".github/workflows/ci.yml": [
                "pip install coverage==7.16.0 ruff==0.16.5",
                "pip install coverage==7.16.0",
            ],
            ".github/workflows/maintenance-audit.yml": [
                "pip install coverage==7.16.0",
            ],
            ".github/workflows/update-homebrew-formula.yml": [
                "pip install coverage==7.16.0",
            ],
        }

        actual_commands = {}
        workflow_dir = ROOT / ".github/workflows"
        for workflow_path in sorted(workflow_dir.iterdir()):
            if workflow_path.suffix not in {".yml", ".yaml"}:
                continue
            commands = []
            for line in workflow_path.read_text().splitlines():
                command = line.strip()
                if command.startswith("run: pip install "):
                    command = command.removeprefix("run: ")
                if command.startswith("pip install "):
                    commands.append(command)
            if commands:
                relative_path = str(workflow_path.relative_to(ROOT))
                actual_commands[relative_path] = commands

        self.assertEqual(actual_commands, expected_commands)
