"""Tests for tests/_containerfile.py, the parser the Containerfile tests rely on.

A parser used as a test oracle is only worth what its own failures are worth:
one that quietly accepted a malformed file, or guessed at a construct it does
not really support, would make every assertion built on it weaker than the
substring assertions it replaced.
"""

import unittest

from _containerfile import ContainerfileError
from _containerfile import parse as parse_containerfile


class ContainerfileParserTests(unittest.TestCase):
    def test_parses_stages_copies_and_a_continued_run(self) -> None:
        instructions = parse_containerfile(
            "# a comment\n"
            "FROM scratch AS ctx\n"
            "COPY build_files /\n"
            "\n"
            "FROM quay.io/fedora-ostree-desktops/silverblue:43\n"
            "\n"
            "RUN --mount=type=bind,from=ctx,source=/,target=/ctx \\\n"
            "    --mount=type=tmpfs,dst=/tmp \\\n"
            "    /ctx/build.sh\n"
        )
        self.assertEqual(
            [(item.keyword, item.line) for item in instructions],
            [("FROM", 2), ("COPY", 3), ("FROM", 5), ("RUN", 7)],
        )
        self.assertEqual(instructions[0].image, "scratch")
        self.assertEqual(instructions[0].stage, "ctx")
        self.assertEqual(instructions[1].sources, ("build_files",))
        self.assertEqual(instructions[1].destination, "/")
        self.assertIsNone(instructions[2].stage)
        self.assertEqual(instructions[2].image, "quay.io/fedora-ostree-desktops/silverblue:43")
        # The continuation lines are joined, so the command is what the build
        # actually runs rather than the first physical line of it.
        self.assertEqual(instructions[3].argument, "/ctx/build.sh")
        self.assertEqual(
            instructions[3].mounts(),
            (
                {"type": "bind", "from": "ctx", "source": "/", "target": "/ctx"},
                {"type": "tmpfs", "dst": "/tmp"},
            ),
        )

    def test_joins_a_multi_command_run_into_one_argument(self) -> None:
        (instruction,) = parse_containerfile(
            "RUN --mount=type=cache,dst=/var/cache \\\n"
            "    /usr/bin/systemctl preset brew-setup.service && \\\n"
            "    /usr/bin/systemctl preset brew-update.timer\n"
        )
        self.assertEqual(
            instruction.argument,
            "/usr/bin/systemctl preset brew-setup.service && "
            "/usr/bin/systemctl preset brew-update.timer",
        )

    def test_reads_a_copy_flag_by_name(self) -> None:
        (instruction,) = parse_containerfile("COPY --from=ghcr.io/ublue-os/brew:latest /system_files /\n")
        self.assertEqual(instruction.flag("from"), "ghcr.io/ublue-os/brew:latest")
        self.assertEqual(instruction.sources, ("/system_files",))
        self.assertEqual(instruction.destination, "/")

    def test_a_missing_flag_is_an_error_not_a_default(self) -> None:
        (instruction,) = parse_containerfile("COPY build_files /\n")
        with self.assertRaises(ContainerfileError):
            instruction.flag("from")

    def test_a_double_dash_inside_a_command_is_not_read_as_a_flag(self) -> None:
        (instruction,) = parse_containerfile("RUN bootc container lint --fatal-warnings\n")
        self.assertEqual(instruction.flags, ())
        self.assertEqual(instruction.argument, "bootc container lint --fatal-warnings")

    def test_rejects_a_continuation_that_runs_off_the_end(self) -> None:
        with self.assertRaises(ContainerfileError):
            parse_containerfile("RUN --mount=type=tmpfs,dst=/tmp \\\n")

    def test_rejects_an_unsupported_instruction(self) -> None:
        with self.assertRaises(ContainerfileError):
            parse_containerfile("ENTRYPOINT /usr/bin/bash\n")

    def test_rejects_an_instruction_with_no_argument(self) -> None:
        with self.assertRaises(ContainerfileError):
            parse_containerfile("FROM\n")
        with self.assertRaises(ContainerfileError):
            parse_containerfile("COPY --from=ctx\n")

    def test_rejects_a_flag_with_no_value(self) -> None:
        with self.assertRaises(ContainerfileError):
            parse_containerfile("COPY --from /system_files /\n")

    def test_rejects_a_malformed_from_and_a_one_operand_copy(self) -> None:
        with self.assertRaises(ContainerfileError):
            parse_containerfile("FROM scratch AS\n")
        with self.assertRaises(ContainerfileError):
            parse_containerfile("FROM scratch AS ctx extra\n")
        with self.assertRaises(ContainerfileError):
            parse_containerfile("COPY build_files\n")

    def test_rejects_a_malformed_mount_field(self) -> None:
        (instruction,) = parse_containerfile("RUN --mount=type=bind,ctx /ctx/build.sh\n")
        with self.assertRaises(ContainerfileError):
            instruction.mounts()


if __name__ == "__main__":
    unittest.main()
