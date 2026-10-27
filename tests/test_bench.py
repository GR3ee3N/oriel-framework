import unittest

from .repository import ROOT, load

bench = load("bench", ROOT / "tools/box/bench/bench.py")


class FakeFrames(bench.Frames):
    def __init__(self, frames):
        super().__init__("/nonexistent")
        self.frames = frames

    @property
    def available(self):
        return True


def frame(drawn, pipeline=0.01):
    return (drawn - pipeline, drawn - pipeline / 2, drawn)


def row(median, low, high, unit="ms", better="lower"):
    return {"suite": "stack", "case": "scroll", "metric": "frame_p95_ms", "unit": unit, "better": better,
            "values": [low, median, high], "median": median, "min": low, "max": high}


class FrameTests(unittest.TestCase):
    def test_smooth_animation_delivers_every_refresh(self):
        frames = FakeFrames([frame(i / 60) for i in range(61)])
        m = bench.frame_metrics(frames, 0, 1, 60)
        self.assertEqual(m["delivered_pct"], 100.0)
        self.assertEqual(m["late"], 0)
        self.assertAlmostEqual(m["frame_max_ms"], 16.7, places=1)

    def test_skipped_refreshes_count_as_late(self):
        times = [i / 60 for i in range(60) if i not in (10, 30, 31)]
        m = bench.frame_metrics(FakeFrames([frame(t) for t in times]), 0, 1, 60)
        self.assertEqual(m["late"], 2)
        self.assertEqual(m["delivered_pct"], 95.0)

    def test_bursts_leave_out_the_still_screen_between_keys(self):
        times = [0, 1 / 60, 2 / 60, 1.0, 1 + 1 / 60]
        m = bench.frame_metrics(FakeFrames([frame(t) for t in times]), 0, 2, 60, bursts=True)
        self.assertLess(m["frame_max_ms"], 20)

    def test_key_latency_uses_the_first_frame_rendered_after_each_key(self):
        frames = FakeFrames([frame(0.05), frame(0.55), frame(1.6)])
        m = bench.key_metrics(frames, [0.0, 0.5, 1.0], 1.5, [0.001, 0.502])
        self.assertEqual(m["key_frame_ms"], 50.0)
        self.assertEqual(m["unanswered"], 1)
        self.assertEqual(m["key_delivery_ms"], 1.5)


class CaseTests(unittest.TestCase):
    scenario = {"steps": [{"name": "open", "keys": ["select"]}, {"name": "back", "keys": ["back"]}]}

    def session(self, cases):
        session = bench.Session.__new__(bench.Session)
        session.args = type("Args", (), {"case": cases})()
        return session

    def test_every_case_runs_by_default(self):
        self.assertEqual(self.session(None).cases("stack"), bench.STACK_CASES)

    def test_named_cases_run_alone(self):
        session = self.session(["scroll", "back"])
        self.assertEqual(session.cases("stack"), ["scroll"])
        self.assertEqual(session.cases("app", self.scenario), ["back"])


class ReportTests(unittest.TestCase):
    def test_overlapping_runs_are_noise(self):
        self.assertEqual(bench.verdict(row(20, 18, 24), row(23, 21, 25)), "noise")

    def test_small_changes_are_unchanged(self):
        self.assertEqual(bench.verdict(row(100, 99, 101), row(102, 101.5, 103)), "same")

    def test_separated_runs_are_a_change(self):
        self.assertEqual(bench.verdict(row(20, 19, 21), row(30, 29, 31)), "worse")
        self.assertEqual(bench.verdict(row(90, 89, 91, "%", "higher"), row(99, 98, 100, "%", "higher")), "better")

    def test_env_needs_a_name_and_value(self):
        self.assertEqual(bench.env_spec("WEBKIT_SKIA_MSAA_SAMPLE_COUNT=0"), "WEBKIT_SKIA_MSAA_SAMPLE_COUNT=0")
        with self.assertRaises(bench.argparse.ArgumentTypeError):
            bench.env_spec("=0")

    def test_report_and_comparison_render(self):
        box = {"model": "Test box", "refresh_hz": 60.0}
        report = {
            "schema": bench.SCHEMA, "created": "2026-10-26T00:00:00+00:00", "label": None, "box": box,
            "software": {"app": "script.example.app"}, "conditions": {}, "settings": {"repeat": 3, "warmup": 1},
            "results": bench.summarise({"stack": {"scroll": {"delivered_pct": [99.0, 98.5, 100.0]}}}),
            "warnings": [],
        }
        self.assertIn("| scroll | 99 (98.5–100) |", bench.render(report))
        self.assertIn("| Environment | unchanged |", bench.render(report))
        self.assertIn("0 better, 0 worse, 1 unchanged", bench.compare(report, report))
        self.assertEqual(bench.regressions(report, report), [])
        worse = dict(report, results=bench.summarise({"stack": {"scroll": {"delivered_pct": [80.0, 79.0, 81.0]}}}))
        self.assertEqual(bench.regressions(report, worse), [("stack", "scroll", "delivered_pct")])


if __name__ == "__main__":
    unittest.main()
