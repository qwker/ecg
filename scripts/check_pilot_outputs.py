"""Read back pilot tables, check invariants, and summarize review priorities."""
import json
from pathlib import Path
import numpy as np
import pandas as pd
from verify_pilot import dump, read_json, sha

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/verification"
LOCAL = ROOT / "private/verification"


def main():
    records = read_json("outputs/verification/record_evidence.json")
    comparisons = read_json("outputs/verification/detector_comparison.json")
    windows = pd.read_parquet(LOCAL / "quality_windows.parquet")
    manifest = read_json("outputs/verification/run_manifest.json")
    assert len(records) == manifest["completed_records"] == 27
    assert manifest["errors"] == 0
    assert sha((ROOT / "scripts/verify_pilot.py").read_bytes()) == manifest["script_sha256"]
    gaps = []
    for r in records:
        table = pd.read_parquet(LOCAL / "beats" / (r["record_id"] + ".parquet"))
        assert len(table) == r["event_count"]
        assert table.nn_valid.isna().all() and table.normal_beat_verified.isna().all()
        assert table.rr_ms.isna().sum() == 1
        assert np.allclose(table.rr_ms.iloc[1:], np.diff(table.sample_index) * 4)
        assert table.event_index.tolist() == list(range(len(table)))
        assert not table.interpolated.any()
        assert table.event_time_local.notna().all() if r["clock_available"] else table.event_time_local.isna().all()
        qw = windows[windows.record_id == r["record_id"]]
        assert qw.event_count.sum() == len(table)
        assert qw.rr_ending_count.sum() == len(table) - 1
        assert qw.rr_fully_contained_count.sum() + qw.rr_crossing_left_boundary.sum() == len(table) - 1
        assert qw.payload_coverage_s.sum() == r["source"]["nb"]["frames"] / 250
        assert qw.window_pass.isna().all() and qw.nn_coverage_s.isna().all()
        assert r["archive_stat_unchanged"]
        gaps.append(dict(record_id=r["record_id"], max_candidate_event_gap_s=float(table.rr_ms.max() / 1000),
                         first_event_s=float(table.offset_seconds.min()), last_event_s=float(table.offset_seconds.max()),
                         span_s=r["source"]["nb"]["frames"] / 250,
                         no_event_full_windows=int(((qw.event_count == 0) & qw.full_payload_window).sum()),
                         rr_change_flags=r["change_flags"], event_count=r["event_count"]))
    stats = {}
    for channel in (2, 7):
        rows = [r for r in comparisons if r["channel"] == channel]
        vendor, detector, matched = [sum(r[key] for r in rows) for key in ("vendor_events", "xqrs_events", "matched")]
        vals = [r["vendor_matched_fraction"] for r in rows if r["vendor_matched_fraction"] is not None]
        stats[str(channel)] = dict(windows=len(rows), vendor=vendor, xqrs=detector, matched=matched,
                                   vendor_fraction=matched / vendor, xqrs_fraction=matched / detector,
                                   median_window_fraction=float(np.median(vals)), below90=sum(v < .9 for v in vals),
                                   no_vendor_events=sum(r["vendor_events"] == 0 for r in rows))
    countstats = {}
    for code, tag in [(1, "SinusNormallHrCount"), (2, "SVRCount"), (3, "VRCount"), (6, "ArtifactCount")]:
        pairs = [(r["vendor_code_counts"].get(str(code), 0), report["counts"][tag])
                 for r in records for report in r["xml_counts"] if report["counts"][tag] is not None]
        diffs = [a - b for a, b in pairs]
        countstats["code%d_minus_%s" % (code, tag)] = dict(n=len(diffs), equal=sum(v == 0 for v in diffs),
              min=min(diffs), max=max(diffs), both_zero=sum(a == b == 0 for a, b in pairs),
              equal_nonzero=sum(a == b and a > 0 for a, b in pairs))
    priority = [{k: v for k, v in r.items() if not k.endswith("_samples")} for r in comparisons
                if r["vendor_events"] == 0 or r["xqrs_events"] == 0 or
                min(r["vendor_matched_fraction"] or 0, r["xqrs_matched_fraction"] or 0) < .9]
    dump(OUT / "rr_gap_diagnostics.json", gaps)
    dump(OUT / "review_priority.json", priority)
    dump(OUT / "completion_checks.json", dict(passed=True, record_tables_checked=len(records),
         detector_summary=stats, report_count_hypotheses=countstats, review_priority_channel_windows=len(priority),
         checks=["RR=diff(original event samples)*4ms", "no removed events or interpolated RR",
                 "NN/normal/window-pass fields remain unknown", "window payload durations and event counts conserve totals",
                 "boundary-crossing RR not counted as contained", "absolute timestamps only when source clock exists",
                 "raw archive stat unchanged", "producer script SHA256 unchanged"],
         verification_script_sha256=sha(Path(__file__).read_bytes())))
    print("Passed: %d record tables; %d windows; %d channel-window review priorities" % (len(records), len(windows), len(priority)))


if __name__ == "__main__":
    main()
