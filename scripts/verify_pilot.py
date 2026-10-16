"""Read-only CCM verification, NOT validated NN/HRV or diagnostic labels.

Run after audit_ccm.py, probe_ccm.py and metadata recovery. Local dated tables
stay under private/verification; outputs/verification contains pseudonymous
review artifacts. No selection or threshold uses diagnostic outcomes.
"""
import collections
import hashlib
import json
from pathlib import Path
import platform
import struct
import time
import xml.etree.ElementTree as ET
import zipfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow
import scipy
import wfdb
from wfdb.processing import xqrs_detect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/verification"
LOCAL = ROOT / "private/verification"
PARAMS = dict(version="pilot-verification-v1", window_seconds=300,
              adjacent_rr_change_flag=0.20, match_tolerance_ms=50,
              detector="WFDB XQRS", detector_channels_zero_based=[1, 6],
              detection_offsets_seconds=[300, 21600, 43200, 64800],
              detection_seconds=60, buffer_seconds=10,
              normal_code=None, timezone=None, rr_interpolation=False)
COUNT_TAGS = ["HRCount", "ArtifactCount", "PCount", "VRCount", "SVRCount",
              "SinusAllHrCount", "SinusNormallHrCount", "SinusBundleBranchBlockCount"]


def read_json(path):
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def clock_grid(start, frames, fs):
    """Half-open clock bins; boundary partial bins retained, never counted full."""
    offset = 0 if start is None else int((pd.Timestamp(start) - pd.Timestamp(start).floor("5min")).total_seconds() * fs)
    width = 300 * fs
    return [(a, a + width, max(a, 0), min(a + width, frames))
            for a in range(-offset, frames, width)]


def match_events(a, b, tolerance_samples):
    """Ordered one-to-one matching; fractions measure agreement, not accuracy."""
    i = j = 0
    differences = []
    while i < len(a) and j < len(b):
        delta = int(b[j]) - int(a[i])
        if abs(delta) <= tolerance_samples:
            differences.append(delta)
            i += 1
            j += 1
        elif delta < 0:
            j += 1
        else:
            i += 1
    return differences


def select_field(evidence, key):
    """No silent preference in a conflict. Keep original sources separately."""
    cfg = evidence.get("config", {}).get(key, {})
    xml = evidence.get("xml", {}).get(key, {})
    if cfg.get("status") == "conflict" or xml.get("status") == "conflict":
        return None, "conflict"
    if cfg.get("status") == "valid" and xml.get("status") == "valid" and cfg["value"] != xml["value"]:
        return None, "conflict"
    if cfg.get("status") == "valid":
        return cfg, "config_xml_agree" if xml.get("status") == "valid" else "config_only"
    if evidence["config_missing"] and xml.get("status") == "valid":
        return xml, "xml_validated_mapping"
    return None, "unavailable"


def build_status(audit, evidence):
    duplicates = {x["record_id"] for x in read_json("outputs/audit/duplicate_hash_verification.json")}
    rows = []
    for r in audit:
        e = evidence[r["record_id"]]
        start, source = select_field(e, "start")
        identity, id_source = select_field(e, "identity")
        span = r["waveform_candidates"][0]["candidate_span_h"]
        rows.append(dict(record_id=r["record_id"], directory_label=r["category"],
                         start_local=None if start is None else start["value"], start_source=source,
                         timezone=None, patient_id_candidate=None if identity is None else identity.get("patient_candidate"),
                         identity_source=id_source, confirmed_patient_id=None, confirmed_label=None,
                         span_hours=span, span_ge18h=span >= 18, span_ge22h=span >= 22,
                         exact_duplicate_waveform= r["record_id"] in duplicates,
                         clinical_hrv_eligible=False, normal_beat_semantics_verified=False))
    groups = collections.defaultdict(list)
    for r in rows:
        if r["patient_id_candidate"]:
            groups[r["patient_id_candidate"]].append(r)
    for r in rows:
        group = groups.get(r["patient_id_candidate"], [])
        r["same_identity_record_count"] = len(group) if group else None
        r["cross_directory_identity"] = len({x["directory_label"] for x in group}) > 1
        reasons = []
        for condition, reason in [(r["start_local"] is None, "missing_or_conflicting_clock"),
                                  (not r["patient_id_candidate"], "unresolved_patient_identity"),
                                  (r["cross_directory_identity"], "cross_directory_label_review"),
                                  (r["exact_duplicate_waveform"], "exact_duplicate_label_review"),
                                  (not r["span_ge18h"], "span_below_proposed_18h")]:
            if condition:
                reasons.append(reason)
        r["record_hold_reasons"] = reasons
        r["global_pending_requirements"] = ["clinical_label_confirmation", "normal_beat_validation", "signal_quality_validation"]
    dump(LOCAL / "record_status.json", rows)
    return {r["record_id"]: r for r in rows}


def read_events(z):
    result, provenance = {}, {}
    for suffix, version, stride in [("ba", 2, 44), ("bd", 1, 48)]:
        infos = [i for i in z.infolist() if i.filename.lower().endswith("." + suffix)]
        if len(infos) != 1:
            raise ValueError("nonunique_" + suffix)
        raw = z.read(infos[0])
        actual_version = struct.unpack_from("<I", raw)[0]
        actual_stride, count = struct.unpack_from("<II", raw, 14)
        if (actual_version, actual_stride, len(raw)) != (version, stride, 4096 + count * stride):
            raise ValueError("unsupported_" + suffix + "_layout")
        result[suffix] = np.frombuffer(raw, dtype="<i4", offset=4096).reshape(count, stride // 4)
        provenance[suffix] = dict(member_index=z.infolist().index(infos[0]), sha256=sha(raw), crc_verified=True,
                                  version=version, count=count, stride=stride)
    if len(result["ba"]) != len(result["bd"]):
        raise ValueError("ba_bd_count_mismatch")
    return result["ba"][:, 4].astype(np.int64), result["bd"][:, 0].copy(), provenance


def report_counts(z):
    reports = []
    for index, info in enumerate(z.infolist()):
        if not info.filename.lower().endswith(".xml"):
            continue
        raw = z.read(info)
        root = ET.fromstring(raw)
        fields = {}
        for tag in COUNT_TAGS:
            vals = {(el.text or "").strip() for el in root.iter() if el.tag.split("}")[-1] == tag}
            fields[tag] = int(next(iter(vals))) if len(vals) == 1 and next(iter(vals)).isdigit() else None
        reports.append(dict(member_index=index, sha256=sha(raw), counts=fields))
    return reports


def rr_table(events, codes, fs, status):
    rr = np.r_[np.nan, np.diff(events) * 1000 / fs]
    flag = np.zeros(len(events), dtype=bool)
    flag[2:] = np.abs(rr[2:] - rr[1:-1]) / rr[1:-1] > PARAMS["adjacent_rr_change_flag"]
    times = pd.to_timedelta(events / fs, unit="s")
    table = pd.DataFrame(dict(record_id=status["record_id"], patient_id_candidate=status["patient_id_candidate"],
                             event_index=np.arange(len(events)), sample_index=events, offset_seconds=events / fs,
                             event_time_local=pd.NaT if status["start_local"] is None else pd.Timestamp(status["start_local"]) + times,
                             rr_ms=rr, vendor_code_raw=codes, previous_vendor_code_raw=pd.array([None] + codes[:-1].tolist(), dtype="Int32"),
                             rr_change_gt20pct=flag, normal_beat_verified=pd.array([None] * len(events), dtype="boolean"),
                             nn_valid=pd.array([None] * len(events), dtype="boolean"), interpolated=False,
                             source="BA.int32_column4_BD.int32_column0_row_alignment_hypothesis",
                             validation_status="unverified_normal_type_and_event_semantics"))
    return table, flag


def quality_windows(wave, events, flags, fs, status):
    rows = []
    for a, b, lo, hi in clock_grid(status["start_local"], len(wave), fs):
        left, right = np.searchsorted(events, [lo, hi])
        contained = max(0, right - max(left, 1))
        # RR ending in this bin but starting before it is counted separately.
        cross = int(left > 0 and left < right and events[left - 1] < lo)
        segment = wave[lo:hi]
        rows.append(dict(record_id=status["record_id"], grid="local_clock" if status["start_local"] else "relative_only",
                         window_start_local=None if not status["start_local"] else str(pd.Timestamp(status["start_local"]) + pd.Timedelta(seconds=a / fs)),
                         window_start_offset_s=a / fs, window_end_offset_s=b / fs,
                         payload_coverage_s=(hi - lo) / fs, full_payload_window=(hi - lo == 300 * fs),
                         event_count=int(right - left), rr_ending_count=contained,
                         rr_fully_contained_count=contained - cross, rr_crossing_left_boundary=cross,
                         rr_change_gt20pct_count=int(flags[left:right].sum()),
                         all_channels_zero_fraction=float(np.mean(np.all(segment == 0, axis=1))),
                         int16_extreme_sample_fraction=float(np.mean((segment == -32768) | (segment == 32767))),
                         constant_channels=int(np.sum(np.ptp(segment.astype(np.int32), axis=0) == 0)),
                         nn_coverage_s=None, longest_invalid_gap_s=None, window_pass=None,
                         status="payload_and_rr_flags_only_not_valid_signal_or_nn_coverage"))
    return rows


def detection_review(wave, events, fs, rid):
    comparisons, annotations = [], []
    fig, axes = plt.subplots(8, 2, figsize=(16, 15), sharex="row")
    for block, offset in enumerate(PARAMS["detection_offsets_seconds"]):
        lo, hi = (offset - 10) * fs, (offset + 70) * fs
        if hi > len(wave):
            for row in range(block * 2, block * 2 + 2):
                for ax in axes[row]:
                    ax.set_visible(False)
            continue
        fragment = wave[lo:hi]
        clip_id = rid + "_" + str(offset)
        # Full 80s, all 8 stored channels available for independent review.
        np.savez_compressed(OUT / "clips" / (clip_id + ".npz"), raw_counts=fragment, fs=fs, start_sample=lo)
        annotations.append(dict(clip_id=clip_id, record_id=rid, file="clips/" + clip_id + ".npz",
                                review_start_sample=offset * fs, review_end_sample=(offset + 60) * fs,
                                human_r_sample_indices=None, human_beat_types=None, reviewer=None,
                                status="awaiting_independent_review"))
        vendor = events[(events >= offset * fs) & (events < (offset + 60) * fs)]
        for c, channel in enumerate(PARAMS["detector_channels_zero_based"]):
            detected = xqrs_detect(fragment[:, channel].astype(float), fs=fs, verbose=False) + lo
            detected = detected[(detected >= offset * fs) & (detected < (offset + 60) * fs)]
            diff = match_events(vendor, detected, fs * PARAMS["match_tolerance_ms"] / 1000)
            abs_ms = np.abs(diff) * 1000 / fs
            comparisons.append(dict(record_id=rid, offset_s=offset, channel=channel + 1, vendor_events=len(vendor),
                                    xqrs_events=len(detected), matched=len(diff),
                                    vendor_matched_fraction=len(diff) / len(vendor) if len(vendor) else None,
                                    xqrs_matched_fraction=len(diff) / len(detected) if len(detected) else None,
                                    median_abs_error_ms=float(np.median(abs_ms)) if len(diff) else None,
                                    max_abs_error_ms=float(np.max(abs_ms)) if len(diff) else None,
                                    vendor_samples=vendor.tolist(), xqrs_samples=detected.tolist(),
                                    status="algorithm_agreement_not_accuracy"))
            start, end = offset * fs, (offset + 10) * fs
            for col in range(2):
                ax = axes[block * 2 + c, col]
                ax.plot(np.arange(start, end) / fs, wave[start:end, channel], lw=.7, color="#24496a")
                if col:
                    for peaks, color in [(vendor, "#cc6031"), (detected, "#27834d")]:
                        for p in peaks[(peaks >= start) & (peaks < end)]:
                            ax.axvline(p / fs, color=color, alpha=.55, lw=.8)
                ax.set_ylabel("Ch%d raw count" % (channel + 1))
                ax.set_title(("Unmarked waveform" if col == 0 else "Orange: vendor; green: XQRS") + " | offset %ds" % offset, fontsize=9)
                ax.grid(alpha=.15)
    fig.suptitle(rid + " | 10s previews; full 60s review + buffers in NPZ\nStored channels, gain and lead names unknown; neither algorithm is reference truth", fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, .96))
    fig.savefig(OUT / "review" / (rid + ".png"), dpi=110)
    plt.close(fig)
    return comparisons, annotations


def main():
    for directory in [OUT, OUT / "clips", OUT / "review", LOCAL, LOCAL / "beats"]:
        directory.mkdir(parents=True, exist_ok=True)
    start_time = time.time()
    audit = [json.loads(line) for line in (ROOT / "outputs/audit/records.jsonl").read_text(encoding="utf-8").splitlines()]
    evidence = {e["record_id"]: e for e in read_json("private/metadata/source_evidence.json")}
    statuses = build_status(audit, evidence)
    paths = {x["record_id"]: ROOT / x["relative_path"] for x in read_json("private/record_paths.json")}
    summaries, comparisons, annotations, windows, errors = [], [], [], [], []
    for index, selection in enumerate(read_json("outputs/probe/evidence.json")):
        rid = selection["record_id"]
        path = paths[rid]
        before = (path.stat().st_size, path.stat().st_mtime_ns)
        try:
            with zipfile.ZipFile(path) as z:
                events, codes, provenance = read_events(z)
                infos = [i for i in z.infolist() if i.filename.lower().endswith(".nb")]
                if len(infos) != 1:
                    raise ValueError("nonunique_nb")
                raw = z.read(infos[0])  # Full read verifies this ZIP member's CRC.
                version, fs, frames = struct.unpack_from("<III", raw)
                if (version, fs, len(raw)) != (0, 250, 12 + frames * 16):
                    raise ValueError("unsupported_nb_layout")
                wave = np.frombuffer(raw, dtype="<i2", offset=12).reshape(-1, 8)
                if np.any(np.diff(events) <= 0) or np.any(events < 0) or np.any(events >= frames):
                    raise ValueError("invalid_event_order_or_range")
                provenance["nb"] = dict(member_index=z.infolist().index(infos[0]), sha256=sha(raw), crc_verified=True, fs=fs, frames=frames)
                reports = report_counts(z)
                table, flags = rr_table(events, codes, fs, statuses[rid])
                table.to_parquet(LOCAL / "beats" / (rid + ".parquet"), index=False)
                quality = quality_windows(wave, events, flags, fs, statuses[rid])
                windows.extend(quality)
                compare, review = detection_review(wave, events, fs, rid)
                comparisons.extend(compare)
                annotations.extend(review)
                code_counts = {str(int(k)): int(v) for k, v in zip(*np.unique(codes, return_counts=True))}
                differences = []
                for report in reports:
                    rc = report["counts"]
                    hypotheses = {"code1_minus_SinusNormallHrCount": (code_counts.get("1", 0), rc["SinusNormallHrCount"]),
                                  "code2_minus_SVRCount": (code_counts.get("2", 0), rc["SVRCount"]),
                                  "code3_minus_VRCount": (code_counts.get("3", 0), rc["VRCount"]),
                                  "code6_minus_ArtifactCount": (code_counts.get("6", 0), rc["ArtifactCount"])}
                    differences.append({k: None if b is None else a - b for k, (a, b) in hypotheses.items()})
                summaries.append(dict(record_id=rid, source=provenance, event_count=len(events),
                                      rr_count=len(events) - 1, change_flags=int(flags.sum()),
                                      vendor_code_counts=code_counts, xml_counts=reports, count_hypothesis_differences=differences,
                                      window_count=len(quality), full_payload_windows=sum(w["full_payload_window"] for w in quality),
                                      clock_available=statuses[rid]["start_local"] is not None,
                                      archive_stat_unchanged=before == (path.stat().st_size, path.stat().st_mtime_ns),
                                      normal_code=None, nn_validated=False))
                del wave, raw, table
        except Exception as exc:
            errors.append(dict(record_id=rid, error_type=type(exc).__name__, error=str(exc)))
        print("Verified %d/27 %s; errors=%d" % (index + 1, rid, len(errors)), flush=True)
    pd.DataFrame(windows).to_parquet(LOCAL / "quality_windows.parquet", index=False)
    dump(OUT / "record_evidence.json", summaries)
    dump(OUT / "detector_comparison.json", comparisons)
    dump(OUT / "independent_review_template.json", annotations)
    dump(OUT / "errors.json", errors)
    holds = collections.Counter(reason for row in statuses.values() for reason in row["record_hold_reasons"])
    manifest = dict(parameters=PARAMS, completed_records=len(summaries), errors=len(errors),
                    total_inventory=len(statuses), record_hold_counts=dict(holds),
                    rr_count=sum(x["rr_count"] for x in summaries), windows=len(windows),
                    detector_comparisons=len(comparisons), independent_review_clips=len(annotations),
                    manual_reviews_completed=0, clinical_hrv_eligible=0, elapsed_s=round(time.time() - start_time, 1),
                    versions=dict(python=platform.python_version(), numpy=np.__version__, scipy=scipy.__version__,
                                  pandas=pd.__version__, pyarrow=pyarrow.__version__, matplotlib=matplotlib.__version__, wfdb=wfdb.__version__),
                    script_sha256=sha(Path(__file__).read_bytes()),
                    inputs_sha256={p: sha((ROOT / p).read_bytes()) for p in ["README.md", "outputs/probe/evidence.json", "private/metadata/source_evidence.json", "private/record_paths.json", "outputs/audit/records.jsonl"]},
                    all_raw_archives_untouched=all(s["archive_stat_unchanged"] for s in summaries))
    dump(OUT / "run_manifest.json", manifest)
    if errors:
        raise SystemExit("Partial run: inspect errors.json")


if __name__ == "__main__":
    main()
