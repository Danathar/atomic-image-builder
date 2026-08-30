import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from coverage_badge import badge_color, badge_payload, main, trend_row


class BadgeColorTests(unittest.TestCase):
    def test_at_or_above_high_is_brightgreen(self) -> None:
        self.assertEqual(badge_color(90), "brightgreen")
        self.assertEqual(badge_color(100), "brightgreen")

    def test_between_low_and_high_is_yellow(self) -> None:
        self.assertEqual(badge_color(89), "yellow")
        self.assertEqual(badge_color(75), "yellow")

    def test_below_low_is_red(self) -> None:
        self.assertEqual(badge_color(74), "red")
        self.assertEqual(badge_color(0), "red")

    def test_custom_thresholds_are_honoured(self) -> None:
        self.assertEqual(badge_color(50, high=60, low=40), "yellow")
        self.assertEqual(badge_color(39, high=60, low=40), "red")


class BadgePayloadTests(unittest.TestCase):
    def test_matches_the_shields_io_endpoint_schema(self) -> None:
        self.assertEqual(
            badge_payload(100, label="unit coverage"),
            {
                "schemaVersion": 1,
                "label": "unit coverage",
                "message": "100%",
                "color": "brightgreen",
            },
        )

    def test_uses_the_same_thresholds_as_badge_color(self) -> None:
        self.assertEqual(badge_payload(50)["color"], "red")


class TrendRowTests(unittest.TestCase):
    def test_formats_a_csv_line(self) -> None:
        self.assertEqual(
            trend_row("2026-08-30", "abc1234", 91),
            "2026-08-30,abc1234,91",
        )


class MainTests(unittest.TestCase):
    def run_main(self, out_dir: Path = None) -> tuple[int, Path, Path]:
        if out_dir is None:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            out_dir = Path(tmp.name)
        badge_out = out_dir / "coverage-unit.json"
        trend_out = out_dir / "coverage-trend.csv"
        args = [
            "91",
            "--date",
            "2026-08-30",
            "--sha",
            "abc1234",
            "--badge-out",
            str(badge_out),
            "--trend-out",
            str(trend_out),
        ]
        with contextlib.redirect_stderr(io.StringIO()):
            status = main(args)
        return status, badge_out, trend_out

    def test_writes_the_badge_json(self) -> None:
        status, badge_out, _ = self.run_main()
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads(badge_out.read_text()),
            {
                "schemaVersion": 1,
                "label": "unit coverage",
                "message": "91%",
                "color": "brightgreen",
            },
        )

    def test_appends_rather_than_overwrites_the_trend_file(self) -> None:
        _, _, trend_out = self.run_main()
        # A pre-existing row simulates an earlier CI run on an earlier
        # commit; a second run against the same file must not clobber it.
        with open(trend_out, "a", encoding="utf-8") as f:
            f.write("2026-08-29,def5678,90\n")
        self.run_main(out_dir=trend_out.parent)
        lines = trend_out.read_text().splitlines()
        self.assertIn("2026-08-29,def5678,90", lines)
        self.assertIn("2026-08-30,abc1234,91", lines)

    def test_rejects_a_percent_outside_zero_to_a_hundred(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        badge_out = Path(tmp.name) / "coverage-unit.json"
        trend_out = Path(tmp.name) / "coverage-trend.csv"
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                main(
                    [
                        "101",
                        "--date",
                        "2026-08-30",
                        "--sha",
                        "abc1234",
                        "--badge-out",
                        str(badge_out),
                        "--trend-out",
                        str(trend_out),
                    ]
                )
        self.assertEqual(ctx.exception.code, 2)
        self.assertFalse(badge_out.exists())


if __name__ == "__main__":
    unittest.main()
