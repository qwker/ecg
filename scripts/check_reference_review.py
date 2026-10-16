"""Validate the frozen packet without reading raw CCM or accepting annotations."""
import collections
import json
from pathlib import Path
import re
import numpy as np
import pandas as pd
from PIL import Image
from prepare_reference_review import merged_intervals
from verify_pilot import dump, sha

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/reference_review"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    manifest = read(OUT / "selection_manifest.json")
    status = read(OUT / "review_status.json")
    assert sha((OUT / "selection_manifest.json").read_bytes()) == status["selection_manifest_sha256"]
    for name, digest in manifest["input_sha256"].items():
        assert sha((ROOT / name).read_bytes()) == digest, name
    group_splits = collections.defaultdict(set)
    for g in manifest["record_grouping"].values():
        group_splits[g["group_id"]].add(g["split"])
        if not g["identity_supported"]:
            assert g["split"] == "development"
    assert all(len(splits) == 1 for splits in group_splits.values())
    cases = {c["case_id"]: c for c in manifest["clips"]}
    assert len(cases) == len(manifest["clips"]) == status["cases"]
    assert len({(c["record_id"], c["eval_start"]) for c in cases.values()}) == len(cases)
    refs = read(OUT / "blind/reference_template.json")
    assert len(refs) == len(cases)
    extraction = {r["case_id"]: r for r in read(OUT / "extraction_integrity.json")}
    artifacts = []
    for ref in refs:
        case = cases[ref["case_id"]]
        folder = OUT / "blind" / ref["case_id"]
        assert ref["events"] is None and ref["readable_intervals"] is None and ref["reviewer_id"] is None
        assert ref["adjudication_status"] == "not_reviewed"
        digest = sha((folder / "waveform.npz").read_bytes())
        assert digest == ref["waveform_sha256"]
        expected = case["source_npz_sha256"] if case["source_npz"] else extraction[case["case_id"]]["npz_sha256"]
        assert digest == expected
        with np.load(folder / "waveform.npz", allow_pickle=False) as data:
            assert set(data.files) == {"raw_counts", "fs", "start_sample"}
            assert data["raw_counts"].shape == (case["data_end"] - case["data_start"], 8)
            assert int(data["fs"]) == 250 and int(data["start_sample"]) == case["data_start"]
        assert case["eval_end"] - case["eval_start"] == 15000
        assert case["data_start"] <= case["eval_start"] < case["eval_end"] <= case["data_end"]
        files = sorted(folder.glob("*.png"))
        assert len(files) == 7
        files.append(OUT / "comparison" / (case["case_id"] + ".png"))
        for path in files:
            with Image.open(path) as im:
                assert im.size == (1650, 1210)
                im.verify()
            artifacts.append(dict(path=str(path.relative_to(OUT)), sha256=sha(path.read_bytes())))
    for path in (OUT / "blind").rglob("*.html"):
        contents = path.read_text(encoding="utf-8")
        assert not re.search(r"r_[0-9a-f]{20}|person_[0-9a-f]+|vendor_code|xqrs_samples|comparison/", contents)
        for name in re.findall(r'(?:src|href)="([^"]+)"', contents):
            if not name.startswith("#"):
                assert (path.parent / name).exists(), name
    for gap in read(OUT / "gap_review.json"):
        intervals = gap["sampled_intervals"] + gap["unsampled_intervals"]
        assert sum(b - a for a, b in intervals) == gap["end_sample"] - gap["start_sample"]
        assert merged_intervals(intervals) == [[gap["start_sample"], gap["end_sample"]]]
        assert gap["human_reviewed_intervals"] == [] and gap["nn_valid"] is None
    gap_sidecar = pd.read_parquet(OUT / "event_gap_candidates.parquet")
    expected_gaps = [g for g in read(OUT / "gap_review.json") if g["kind"] == "between_events"]
    assert len(gap_sidecar) == len(expected_gaps) == 10
    assert gap_sidecar.event_gap_candidate.all() and gap_sidecar.nn_valid.isna().all()
    assert gap_sidecar.right_event_index.tolist() == [g["right_event_index"] for g in expected_gaps]
    window_index = read(OUT / "window_review_index.json")
    old_windows = pd.read_parquet(ROOT / "private/verification/quality_windows.parquet")
    expected_windows = old_windows[(old_windows.event_count == 0) | (~old_windows.full_payload_window)]
    assert len(window_index) == len(expected_windows) == 345
    for entry, (_, old) in zip(window_index, expected_windows.iterrows()):
        assert entry["record_id"] == old.record_id and entry["start_offset_s"] == old.window_start_offset_s
        assert entry["event_count"] == old.event_count and entry["nn_valid"] is None
    observations = read(OUT / "engineering_observations.json")["observations"]
    for observation in observations:
        assert not observation["independent_reference"] and not observation["human_reference_completed"]
        assert cases[observation["case_id"]]["group"]["split"] == "development"
        assert (OUT / observation["image"]).exists()
    for rid, digest in manifest["beat_table_sha256"].items():
        path = ROOT / "private/verification/beats" / (rid + ".parquet")
        assert sha(path.read_bytes()) == digest
        table = pd.read_parquet(path, columns=["event_index", "vendor_code_raw"])
        assert table.loc[table.vendor_code_raw == 99, "event_index"].tolist() == list(range(6))
    code = read(OUT / "code_mapping_evidence.json")
    assert {e["raw_code"] for e in code["events"]} == {1, 2, 3, 6, 13, 99}
    assert not code["mappings_confirmed"]
    for e in code["events"]:
        assert e["ba_raw_row"][4] == e["sample_index"] and e["bd_raw_row"][0] == e["raw_code"]
        assert e["normal_beat_verified"] is None
    assert not status["nn_rules_frozen"] and not status["clinical_hrv_released"]
    dump(OUT / "completion_checks.json", dict(passed=True, cases=len(cases), png_files=len(artifacts),
         checks=["input hashes and frozen selection", "no group split leakage under known relationships",
                 "raw NPZ copy hash and sample boundaries", "all image files decode and local links resolve",
                 "blind HTML contains no record identifiers or algorithm positions", "blank reference remains blank",
                 "sampled/unsampled gap intervals conserve time", "prior RR tables unchanged",
                 "all six raw codes have row-level evidence", "code99 exactly first six rows in all 27 records",
                 "gap sidecar and 345 window indices match prior data", "AI observations are not human reference"],
         supplementary_sha256={name: sha((OUT / name).read_bytes()) for name in ["event_gap_candidates.parquet", "window_review_index.json", "engineering_observations.json"]},
         quality_window_source_sha256=sha((ROOT / "private/verification/quality_windows.parquet").read_bytes()),
         artifacts=artifacts, checker_sha256=sha(Path(__file__).read_bytes())))
    print("Passed: %d cases, %d PNGs; reference and NN states remain unverified" % (len(cases), len(artifacts)))


if __name__ == "__main__":
    main()
