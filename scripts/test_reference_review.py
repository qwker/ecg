import unittest
from prepare_reference_review import complement, grouping, merged_intervals


class ReferencePreparationTests(unittest.TestCase):
    def test_sampling_does_not_cover_unseen_gap(self):
        self.assertEqual(complement(0, 1000, [(0, 60), (400, 460), (940, 1000)]), [[60, 400], [460, 940]])

    def test_overlap_and_boundary_clipping(self):
        self.assertEqual(merged_intervals([(20, 40), (10, 30), (40, 50), (9, 9)]), [[10, 50]])
        self.assertEqual(complement(0, 100, [(-10, 30), (20, 60), (90, 120)]), [[60, 90]])

    def test_identity_and_waveform_transitive_grouping(self):
        records = {r: {"source": {"nb": {"sha256": wave}}} for r, wave in [("a", "x"), ("b", "y"), ("c", "y"), ("d", "z")]}
        status = {r: {"patient_id_candidate": identity} for r, identity in [("a", "p1"), ("b", "p1"), ("c", "p2"), ("d", None)]}
        result = grouping(records, status)
        self.assertEqual(result["a"]["group_id"], result["c"]["group_id"])
        self.assertEqual(result["a"]["split"], result["c"]["split"])
        self.assertEqual(result["d"]["split"], "development")
        self.assertFalse(result["d"]["generalization_claim_allowed"])


if __name__ == "__main__":
    unittest.main()
