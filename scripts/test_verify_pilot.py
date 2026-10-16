"""Boundary tests for provisional time grid, matching and source conflicts."""
import unittest
import numpy as np
from verify_pilot import clock_grid, match_events, rr_table, select_field


class VerificationTests(unittest.TestCase):
    def test_midnight_and_partial_bins(self):
        bins = clock_grid("2025-01-01 23:59:59", 86400 * 250, 250)
        self.assertEqual(len(bins), 289)
        self.assertEqual(sum(hi - lo for _, _, lo, hi in bins), 86400 * 250)
        self.assertEqual(sum(hi - lo == 300 * 250 for _, _, lo, hi in bins), 287)
        self.assertEqual(bins[0][3] - bins[0][2], 250)

    def test_no_clock_remains_relative(self):
        bins = clock_grid(None, 600 * 250, 250)
        self.assertEqual(len(bins), 2)
        self.assertEqual(bins[0][0], 0)

    def test_one_to_one_and_tolerance(self):
        self.assertEqual(match_events([100, 110, 300], [105, 312, 313], 12.5), [5, 12])
        self.assertEqual(match_events([100], [113], 12.5), [])

    def test_flags_keep_events_and_unknown_nn(self):
        events = np.array([100, 350, 850, 1100])
        table, flags = rr_table(events, np.array([1, 1, 3, 1]), 250,
                                dict(record_id="test", patient_id_candidate=None, start_local=None))
        self.assertEqual(table.sample_index.tolist(), events.tolist())
        self.assertEqual(flags.tolist(), [False, False, True, True])
        self.assertTrue(table.nn_valid.isna().all())
        self.assertEqual(table.rr_ms.iloc[2], 2000)

    def test_conflict_no_source_preference(self):
        e = dict(config_missing=False, config={"start": dict(status="valid", value="a")},
                 xml={"start": dict(status="valid", value="b")})
        self.assertEqual(select_field(e, "start"), (None, "conflict"))


if __name__ == "__main__":
    unittest.main()
