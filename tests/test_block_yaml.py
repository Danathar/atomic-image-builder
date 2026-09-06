"""Tests for tests/_block_yaml.py, the recipe parser the recipe tests rely on.

A parser used as a test oracle is only worth what its own failures are worth:
one that quietly accepted a malformed document, or guessed at a construct it
does not really support, would make every assertion built on it weaker than
the substring assertions it replaced.
"""

import unittest

from _block_yaml import BlockYamlError
from _block_yaml import parse as parse_block_yaml


class BlockYamlTests(unittest.TestCase):
    def test_parses_nested_mappings_sequences_and_quoted_scalars(self) -> None:
        document = parse_block_yaml(
            "---\n"
            "# a comment\n"
            "name: demo\n"
            "image-version: \"43\"\n"
            "\n"
            "modules:\n"
            "  - type: dnf\n"
            "    install:\n"
            "      packages:\n"
            '        - "htop"\n'
            "  - type: signing\n"
        )
        self.assertEqual(
            document,
            {
                "name": "demo",
                "image-version": "43",
                "modules": [
                    {"type": "dnf", "install": {"packages": ["htop"]}},
                    {"type": "signing"},
                ],
            },
        )

    def test_a_colon_without_a_following_space_stays_part_of_the_scalar(self) -> None:
        # "ghcr.io/ublue-os/brew:latest" is a scalar in YAML, and a package
        # name like "epel:" is one only because generate_recipe quotes it.
        document = parse_block_yaml(
            "snippets:\n"
            "  - COPY --from=ghcr.io/ublue-os/brew:latest /system_files /\n"
        )
        self.assertEqual(
            document,
            {"snippets": ["COPY --from=ghcr.io/ublue-os/brew:latest /system_files /"]},
        )

    def test_literal_block_keeps_its_lines_and_drops_the_common_indent(self) -> None:
        document = parse_block_yaml(
            "snippets:\n"
            "  - |\n"
            "    RUN true \\\n"
            "        && true\n"
        )
        self.assertEqual(document, {"snippets": ["RUN true \\\n    && true"]})

    def test_sequence_at_its_key_indent_parses_like_an_indented_one(self) -> None:
        self.assertEqual(
            parse_block_yaml("modules:\n- type: signing\n"),
            parse_block_yaml("modules:\n  - type: signing\n"),
        )

    def test_rejects_an_indentation_slip(self) -> None:
        with self.assertRaises(BlockYamlError):
            parse_block_yaml("modules:\n  - type: dnf\n     install:\n      packages:\n")

    def test_rejects_a_duplicate_key(self) -> None:
        # Real YAML resolves a duplicate key by keeping the last one, which is
        # how a generator emitting "remove:" twice loses a whole list quietly.
        with self.assertRaises(BlockYamlError):
            parse_block_yaml("remove:\n  packages:\n    - \"a\"\nremove:\n  packages:\n    - \"b\"\n")

    def test_rejects_an_unsupported_flow_collection(self) -> None:
        with self.assertRaises(BlockYamlError):
            parse_block_yaml("packages: [htop, tmux]\n")

    def test_rejects_a_line_that_is_neither_a_key_nor_a_sequence_entry(self) -> None:
        with self.assertRaises(BlockYamlError):
            parse_block_yaml("name: demo\nnot a mapping entry\n")

    def test_an_unquoted_trailing_colon_becomes_a_mapping_node_not_a_string(self) -> None:
        # This is the corruption generate_recipe's quoting prevents: BlueBuild
        # asks for a list of strings and gets a list of one-key mappings.
        self.assertEqual(parse_block_yaml("packages:\n  - epel:\n"), {"packages": [{"epel": None}]})

    def test_empty_document_is_none(self) -> None:
        self.assertIsNone(parse_block_yaml("---\n# nothing else\n"))


if __name__ == "__main__":
    unittest.main()
