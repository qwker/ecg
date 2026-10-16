"""Freeze and render a bounded reference-review packet; never invent labels.

--prepare reuses prior events/NPZ, reads only small metadata and requested new
waveform ranges. --render consumes the frozen selection, with no reselection.
The blind folder is self-contained; coordinator evidence is kept outside it.
"""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import random
import shutil
import struct
import xml.etree.ElementTree as ET
import zipfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from wfdb.processing import xqrs_detect
from verify_pilot import read_json, dump, sha

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/reference_review"
LOCAL = ROOT / "private/reference_review"
FS = 250
SEED = 20261015
PARAMS = dict(version="reference-preparation-v1", seed=SEED, fs=FS,
              event_gap_flag_seconds=5, gap_sampling_min_seconds=300,
              gap_internal_fractions=[.25, .5, .75], routine_per_unique_waveform=1,
              code_record_limit=2, target_codes=[1, 2, 3, 6, 13, 99],
              evaluation_seconds=60, buffer_seconds=10,
              reserve_fraction=.2, grouping="candidate_identity_and_exact_nb_sha256",
              split_status="provisional_not_confirmed_patient_holdout")


def merged_intervals(intervals):
    result = []
    for a, b in sorted(intervals):
        if b <= a:
            continue
        if result and a <= result[-1][1]:
            result[-1][1] = max(b, result[-1][1])
        else:
            result.append([int(a), int(b)])
    return result


def complement(a, b, intervals):
    cursor, out = a, []
    for lo, hi in merged_intervals([(max(a, x), min(b, y)) for x, y in intervals]):
        if lo > cursor:
            out.append([cursor, lo])
        cursor = max(cursor, hi)
    if cursor < b:
        out.append([cursor, b])
    return out


def grouping(records, status):
    parent = {rid: rid for rid in records}
    def find(rid):
        while parent[rid] != rid:
            parent[rid] = parent[parent[rid]]
            rid = parent[rid]
        return rid
    for i, a in enumerate(sorted(records)):
        for b in sorted(records)[i + 1:]:
            pa, pb = status[a]["patient_id_candidate"], status[b]["patient_id_candidate"]
            if (pa and pa == pb) or records[a]["source"]["nb"]["sha256"] == records[b]["source"]["nb"]["sha256"]:
                parent[find(b)] = find(a)
    groups = collections.defaultdict(list)
    for rid in records:
        groups[find(rid)].append(rid)
    result = {}
    # Prior exposed example waveforms cannot become untouched reserved cases.
    exposed = {"r_92e5bc2a63448166fbd1", "r_9e025c62597a9ff90a18", "r_7e978b9f0cae8f5514ab"}
    candidates = [g for g, ids in sorted(groups.items()) if all(status[r]["patient_id_candidate"] for r in ids) and not (set(ids) & exposed)]
    random.Random(SEED).shuffle(candidates)
    reserve = set(candidates[:max(1, round(len(candidates) * .2))])
    for group, ids in groups.items():
        known = all(status[r]["patient_id_candidate"] for r in ids)
        split = "reserved_provisional" if group in reserve else "development"
        for rid in ids:
            result[rid] = dict(group_id="group_" + sha("|".join(sorted(ids)).encode())[:16],
                               split=split, identity_supported=known, linked_records=sorted(ids),
                               generalization_claim_allowed=False)
    return result


def metadata_evidence(rid, z, source_evidence, record):
    """Allowlisted scalar report range/version fields only; no diagnosis text."""
    sources = []
    allowed_xml = {"OriCollectAllSeconds", "AnalysisTime", "AnalysisTimeSeconds", "RealCollectSecondDuration", "UnEffectTimePect"}
    for idx, info in enumerate(z.infolist()):
        suffix = Path(info.filename).suffix.lower()
        if suffix not in (".xml", ".configjson"):
            continue
        raw = z.read(info)
        entry = dict(member_index=idx, kind=suffix, sha256=sha(raw), bytes=len(raw), zip_member_datetime=list(info.date_time))
        if suffix == ".xml":
            root = ET.fromstring(raw)
            node = root.find("AnalysisInfoTotal")
            entry["analysis_fields"] = {} if node is None else {e.tag: e.text for e in node if e.tag in allowed_xml}
        else:
            obj = json.loads(raw.decode("utf-8-sig"))
            detail_rows = []
            for key in ["HolterReportDetail", "RealHolterReportDetail"]:
                detail = obj.get(key)
                if not isinstance(detail, dict):
                    continue
                exam = detail.get("Examination") or {}
                detail_rows.append(dict(node=key, collect_seconds=exam.get("CollectAllSeconds"),
                                        report_status=detail.get("ReportStatus"), holter_report_status=detail.get("HolterReportStatus")))
            entry["report_nodes"] = detail_rows
            entry["version_fields"] = {k: v for k, v in obj.items() if "version" in k.lower() and isinstance(v, (str, int, float, type(None)))}
            entry["beat_color_list_length"] = len(obj.get("BeatColorList") or [])
            entry["color_values_do_not_identify_types"] = True
        sources.append(entry)
    return dict(record_id=rid, sources=sources, prior_field_comparison=source_evidence.get("comparison"),
                ba_version=record["source"]["ba"]["version"], bd_version=record["source"]["bd"]["version"],
                row_counts_match=record["source"]["ba"]["count"] == record["source"]["bd"]["count"],
                row_alignment_semantics_verified=False, exact_analysis_intervals=None,
                report_to_binary_revision_link="unverified", timestamp_note="ZIP dates are not proof of analysis version")


def prepare():
    if (OUT / "selection_manifest.json").exists():
        raise RuntimeError("Frozen selection already exists; use --render, do not silently overwrite it")
    for folder in [OUT, LOCAL, LOCAL / "clips", OUT / "blind", OUT / "comparison"]:
        folder.mkdir(parents=True, exist_ok=True)
    input_names = ["README.md", "private/verification/record_status.json", "outputs/verification/record_evidence.json",
                   "outputs/verification/detector_comparison.json", "outputs/verification/review_priority.json",
                   "outputs/verification/rr_gap_diagnostics.json", "outputs/verification/run_manifest.json",
                   "outputs/verification/independent_review_template.json", "private/metadata/source_evidence.json"]
    input_hashes = {p: sha((ROOT / p).read_bytes()) for p in input_names}
    previous = read_json("outputs/verification/run_manifest.json")
    assert previous["script_sha256"] == sha((ROOT / "scripts/verify_pilot.py").read_bytes())
    records = {r["record_id"]: r for r in read_json("outputs/verification/record_evidence.json")}
    status = {r["record_id"]: r for r in read_json("private/verification/record_status.json")}
    source = {r["record_id"]: r for r in read_json("private/metadata/source_evidence.json")}
    paths = {r["record_id"]: ROOT / r["relative_path"] for r in read_json("private/record_paths.json")}
    templates = read_json("outputs/verification/independent_review_template.json")
    comparisons = read_json("outputs/verification/detector_comparison.json")
    priority = read_json("outputs/verification/review_priority.json")
    groups = grouping(records, status)
    canonical = {}
    for rid in sorted(records):
        digest = records[rid]["source"]["nb"]["sha256"]
        canonical.setdefault(digest, rid)
    representative = {rid: canonical[r["source"]["nb"]["sha256"]] for rid, r in records.items()}
    tables = {rid: pd.read_parquet(ROOT / "private/verification/beats" / (rid + ".parquet"),
                 columns=["event_index", "sample_index", "vendor_code_raw"]) for rid in records}
    table_hashes = {rid: sha((ROOT / "private/verification/beats" / (rid + ".parquet")).read_bytes()) for rid in records}
    existing = {(representative[t["record_id"]], t["review_start_sample"]): t for t in reversed(templates)}
    selected = {}

    def add(rid, start, reason, detail=None):
        rid = representative[rid]
        frames = records[rid]["source"]["nb"]["frames"]
        start = max(0, min(int(start), frames - 60 * FS))
        key = (rid, start)
        if key not in selected:
            selected[key] = dict(record_id=rid, eval_start=start, eval_end=start + 60 * FS,
                                 data_start=max(0, start - 10 * FS), data_end=min(frames, start + 70 * FS),
                                 reasons=[], details=[], group=groups[rid], existing_template=existing.get(key))
        if reason not in selected[key]["reasons"]:
            selected[key]["reasons"].append(reason)
        if detail is not None:
            selected[key]["details"].append(detail)
        return key

    for r in priority:
        add(r["record_id"], r["offset_s"] * FS, "directed_disagreement", dict(source_record=r["record_id"], channel=r["channel"]))
    priority_dedup = len(selected)
    # One high-agreement control per different waveform, balanced across time slots.
    good = collections.defaultdict(list)
    for r in comparisons:
        good[(representative[r["record_id"]], r["offset_s"] * FS)].append(r)
    rng = random.Random(SEED)
    wave_order = sorted(set(representative.values()))
    rng.shuffle(wave_order)
    slot_counts = collections.Counter()
    controls_unavailable = []
    for rid in wave_order:
        candidates = [k for k, rows in good.items() if k[0] == rid and k not in selected and
                      all(r["vendor_events"] > 0 and (r["vendor_matched_fraction"] or 0) >= .9 and
                          (r["xqrs_matched_fraction"] or 0) >= .9 for r in rows)]
        rng.shuffle(candidates)
        candidates.sort(key=lambda k: slot_counts[k[1]])
        if candidates:
            key = candidates[0]
            add(*key, "routine_high_agreement_control")
            slot_counts[key[1]] += 1
        else:
            controls_unavailable.append(rid)
    gaps, gap_id = [], 0
    for rid, t in tables.items():
        samples = t.sample_index.to_numpy()
        bounds = [("before_first_event", 0, int(samples[0]), None),
                  ("after_last_event", int(samples[-1]), records[rid]["source"]["nb"]["frames"], None)]
        bounds += [("between_events", int(samples[i]), int(samples[i + 1]), int(i + 1))
                   for i in np.flatnonzero(np.diff(samples) > PARAMS["event_gap_flag_seconds"] * FS)]
        for kind, a, b, row in bounds:
            gap_id += 1
            gap = dict(gap_id="gap_%04d" % gap_id, record_id=rid, kind=kind, start_sample=a, end_sample=b,
                       duration_seconds=(b - a) / FS, right_event_index=row,
                       waveform_payload_present=True, signal_quality_status=None, beat_validity_status=None,
                       nn_valid=None, human_review_status="not_reviewed", reason="candidate_event_absence_not_physiological_pause", sample_keys=[])
            if b - a >= PARAMS["gap_sampling_min_seconds"] * FS and rid == representative[rid]:
                for role, center in [("left_boundary", a), ("inside_25pct", a + (b - a) * .25),
                                     ("inside_50pct", (a + b) / 2), ("inside_75pct", a + (b - a) * .75), ("right_boundary", b)]:
                    desired = int(center - 30 * FS)
                    # Reuse existing NPZ only if its evaluation interval covers the requested center.
                    matches = [k for k in existing if k[0] == rid and k[1] <= center < k[1] + 60 * FS]
                    key = min(matches, key=lambda k: abs(k[1] + 30 * FS - center)) if matches else (rid, desired)
                    key = add(*key, "long_gap_sampling", dict(gap_id=gap["gap_id"], role=role))
                    gap["sample_keys"].append(list(key))
            gaps.append(gap)
    code_index = []
    for code in PARAMS["target_codes"]:
        candidates = sorted([rid for rid in set(representative.values()) if (tables[rid].vendor_code_raw == code).any()])
        random.Random(SEED + code).shuffle(candidates)
        for rid in candidates[:PARAMS["code_record_limit"]]:
            hits = tables[rid][tables[rid].vendor_code_raw == code]
            indices = sorted(set([0, len(hits) // 2, len(hits) - 1])) if code == 99 else [len(hits) // 2]
            for ix in indices:
                event = hits.iloc[ix]
                sample = int(event.sample_index)
                matches = [k for k in selected if k[0] == rid and k[1] <= sample < k[1] + 60 * FS]
                matches += [k for k in existing if k[0] == rid and k[1] <= sample < k[1] + 60 * FS]
                key = min(matches, key=lambda k: abs(k[1] + 30 * FS - sample)) if matches else (rid, sample - 30 * FS)
                key = add(*key, "code_instance", dict(raw_code=code, event_index=int(event.event_index), sample_index=sample))
                code_index.append(dict(record_id=rid, raw_code=code, event_index=int(event.event_index), sample_index=sample,
                                       selection_key=list(key), code_mapping_status="unverified", reference_beat_type=None,
                                       normal_beat_verified=None, ba_bd_row_alignment_verified=None))
    # Freeze selection before waveform inspection/extraction. Only later integrity metadata is added separately.
    keys = sorted(selected)
    random.Random(SEED).shuffle(keys)
    clips = []
    for i, key in enumerate(keys):
        clip = selected[key]
        clip["case_id"] = "case_%03d" % (i + 1)
        if clip["existing_template"]:
            relative = "outputs/verification/" + clip["existing_template"]["file"]
            clip["source_npz"] = relative
            clip["source_npz_sha256"] = sha((ROOT / relative).read_bytes())
        else:
            clip["source_npz"] = None
            clip["source_nb_sha256"] = records[clip["record_id"]]["source"]["nb"]["sha256"]
        clips.append(clip)
    key_to_case = {(c["record_id"], c["eval_start"]): c["case_id"] for c in clips}
    for g in gaps:
        g["sample_cases"] = [key_to_case[tuple(k)] for k in g.pop("sample_keys")]
        intervals = [(c["eval_start"], c["eval_end"]) for c in clips if c["case_id"] in g["sample_cases"]]
        g["sampled_intervals"] = merged_intervals([(max(a, g["start_sample"]), min(b, g["end_sample"])) for a, b in intervals])
        g["unsampled_intervals"] = complement(g["start_sample"], g["end_sample"], intervals)
        g["human_reviewed_intervals"] = []
    for event in code_index:
        event["case_id"] = key_to_case[tuple(event.pop("selection_key"))]
    dump(OUT / "selection_manifest.json", dict(parameters=PARAMS, input_sha256=input_hashes, beat_table_sha256=table_hashes,
          previous_readme_hash=previous["inputs_sha256"]["README.md"], current_readme_is_revision=True,
          record_grouping=groups, representative_by_record=representative, priority_channel_items=len(priority),
          priority_record_clips=len({(r["record_id"], r["offset_s"]) for r in priority}), priority_waveform_clips=priority_dedup,
          routine_controls_unavailable=controls_unavailable, clips=clips))
    dump(OUT / "gap_review.json", gaps)
    extraction, metadata = [], []
    for rid in sorted(records):
        path = paths[rid]
        before = (path.stat().st_size, path.stat().st_mtime_ns)
        with zipfile.ZipFile(path) as z:
            metadata.append(metadata_evidence(rid, z, source[rid], records[rid]))
            extra = sorted([c for c in clips if c["record_id"] == rid and not c["source_npz"]], key=lambda c: c["data_start"])
            if extra:
                info = z.infolist()[records[rid]["source"]["nb"]["member_index"]]
                with z.open(info) as stream:
                    assert struct.unpack("<III", stream.read(12)) == (0, FS, records[rid]["source"]["nb"]["frames"])
                    for c in extra:
                        stream.seek(12 + c["data_start"] * 16)
                        raw = stream.read((c["data_end"] - c["data_start"]) * 16)
                        assert len(raw) == (c["data_end"] - c["data_start"]) * 16
                        dest = LOCAL / "clips" / (c["case_id"] + ".npz")
                        np.savez_compressed(dest, raw_counts=np.frombuffer(raw, dtype="<i2").reshape(-1, 8), fs=FS, start_sample=c["data_start"])
                        extraction.append(dict(case_id=c["case_id"], source="new_requested_range", payload_sha256=sha(raw),
                                               npz_sha256=sha(dest.read_bytes()), full_member_crc_repeated=False))
            # Read only selected rows of BA/BD. Full member hashes were established in the prior run.
            selected_events = sorted([e for e in code_index if e["record_id"] == rid], key=lambda e: e["event_index"])
            for kind, stride in [("ba", 44), ("bd", 48)]:
                if not selected_events:
                    continue
                with z.open(z.infolist()[records[rid]["source"][kind]["member_index"]]) as stream:
                    for e in selected_events:
                        stream.seek(4096 + e["event_index"] * stride)
                        row = np.frombuffer(stream.read(stride), dtype="<i4").tolist()
                        e[kind + "_raw_row"] = row
                        assert row[4 if kind == "ba" else 0] == e["sample_index" if kind == "ba" else "raw_code"]
            assert before == (path.stat().st_size, path.stat().st_mtime_ns)
        print("Prepared source %d/%d" % (len(metadata), len(records)), flush=True)
    dump(LOCAL / "report_version_evidence.json", metadata)
    dump(OUT / "code_mapping_evidence.json", dict(events=code_index,
          counts=read_json("outputs/verification/completion_checks.json")["report_count_hypotheses"],
          code99_all_record_positions=[dict(record_id=rid, sample_indices=t[t.vendor_code_raw == 99].sample_index.tolist()) for rid, t in tables.items()],
          report_versions="private/reference_review/report_version_evidence.json", mappings_confirmed=[]))
    dump(OUT / "extraction_integrity.json", extraction)
    print("Selection frozen: %d cases, %d new ranges" % (len(clips), len(extraction)))


def render():
    manifest_path = OUT / "selection_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_hash = sha(manifest_path.read_bytes())
    baseline = read_json("outputs/verification/detector_comparison.json")
    tables, templates, artifacts = {}, [], []
    for clip in manifest["clips"]:
        case, rid = clip["case_id"], clip["record_id"]
        source_path = ROOT / clip["source_npz"] if clip["source_npz"] else LOCAL / "clips" / (case + ".npz")
        if clip["source_npz"]:
            assert sha(source_path.read_bytes()) == clip["source_npz_sha256"]
        with np.load(source_path, allow_pickle=False) as data:
            wave, first = data["raw_counts"], int(data["start_sample"])
            assert int(data["fs"]) == FS and wave.shape[1] == 8
        assert first == clip["data_start"] and first + len(wave) == clip["data_end"]
        folder = OUT / "blind" / case
        folder.mkdir(exist_ok=True)
        shutil.copyfile(source_path, folder / "waveform.npz")
        if rid not in tables:
            tables[rid] = pd.read_parquet(ROOT / "private/verification/beats" / (rid + ".parquet"), columns=["sample_index", "vendor_code_raw"])
        t = tables[rid]
        vendor = t[(t.sample_index >= clip["eval_start"]) & (t.sample_index < clip["eval_end"])]
        xqrs = {}
        for ch in [2, 7]:
            prior = [r for r in baseline if r["record_id"] == rid and r["offset_s"] * FS == clip["eval_start"] and r["channel"] == ch]
            peaks = prior[0]["xqrs_samples"] if prior else (xqrs_detect(wave[:, ch - 1].astype(float), FS, verbose=False) + first).tolist()
            xqrs[str(ch)] = [p for p in peaks if clip["eval_start"] <= p < clip["eval_end"]]
        dump(OUT / "comparison" / (case + ".json"), dict(case_id=case, vendor_samples=vendor.sample_index.tolist(),
             vendor_codes=vendor.vendor_code_raw.tolist(), xqrs_samples=xqrs, reference_events=None, is_independent_reference=False))
        # Seven blind PNG pages: buffered overview plus six exact 10s panels.
        views = [("overview", first, first + len(wave))] + [("part_%d" % (n + 1), clip["eval_start"] + n * 10 * FS, clip["eval_start"] + (n + 1) * 10 * FS) for n in range(6)]
        for name, a, b in views:
            fig, axes = plt.subplots(8, 1, figsize=(15, 11), sharex=True)
            segment = wave[a - first:b - first]
            for ch, ax in enumerate(axes):
                ax.plot(np.arange(a, b) / FS, segment[:, ch], lw=.65, color="#24496a")
                ax.set_ylabel("Ch%d\nraw count" % (ch + 1), fontsize=8)
                ax.ticklabel_format(axis="both", style="plain", useOffset=False)
                ax.grid(alpha=.18)
                if name == "overview":
                    ax.axvspan(first / FS, clip["eval_start"] / FS, color="#dbeafe", alpha=.6)
                    ax.axvspan(clip["eval_end"] / FS, (first + len(wave)) / FS, color="#dbeafe", alpha=.6)
                    for boundary in [clip["eval_start"], clip["eval_end"]]:
                        ax.axvline(boundary / FS, color="#555", lw=.7)
            axes[-1].set_xlabel("Seconds from recording start | sample index = seconds x 250")
            fig.suptitle("%s | %s | UNMARKED, stored channels; gain/lead mapping unknown\nEvaluation [%d, %d) samples; blue = buffer only; vertical scales shown per panel" % (case, name, clip["eval_start"], clip["eval_end"]), fontsize=10)
            fig.tight_layout(rect=(0, 0, 1, .95))
            fig.savefig(folder / (name + ".png"), dpi=110)
            # Separate marked overview, never placed in the blind folder.
            if name == "overview":
                for ch, ax in enumerate(axes):
                    for p in vendor.sample_index:
                        ax.axvline(p / FS, color="#c4552c", alpha=.35, lw=.5)
                    for p in xqrs.get(str(ch + 1), []):
                        ax.axvline(p / FS, color="#27834d", alpha=.55, lw=.5)
                fig.suptitle(case + " | COMPARISON ONLY: orange vendor, green XQRS (Ch2/7)\nDo not show before independent reference is frozen", fontsize=11)
                fig.savefig(OUT / "comparison" / (case + ".png"), dpi=110)
            plt.close(fig)
        page = '<!doctype html><meta charset="utf-8"><title>' + case + '</title><style>body{max-width:1650px;margin:auto;font:18px sans-serif}img{width:100%;height:auto}a{margin:12px}p{line-height:1.6}</style>'
        page += '<h1>' + case + '</h1><p>无算法标记。中央 60 秒用于评价，蓝色缓冲区不计入评价。只填写能确认的心搏及可判读区间；未知保持为空。纵轴为原始计数，不是 mV。分图纵轴可不同，请看刻度。</p>'
        page += '<p>采样率 250 Hz；采样索引 = 横轴秒数 ×250。评价区间 [%d, %d)。</p>' % (clip["eval_start"], clip["eval_end"])
        page += ''.join('<a href="#%s">%s</a>' % (name, name) for name, _, _ in views)
        page += ''.join('<h2 id="%s">%s</h2><img loading="lazy" src="%s.png" alt="eight stored channels">' % (name, name, name) for name, _, _ in views)
        (folder / "index.html").write_text(page, encoding="utf-8")
        templates.append(dict(case_id=case, reference_version="reference-v1-unfilled", selection_manifest_sha256=manifest_hash,
            fs=FS, evaluation_start_sample=clip["eval_start"], evaluation_end_sample=clip["eval_end"],
            waveform_sha256=sha((folder / "waveform.npz").read_bytes()), waveform_file=case + "/waveform.npz",
            reviewer_id=None, reviewed_at=None, prior_algorithm_exposure=None, reference_independence_status="not_established",
            readable_intervals=None, unreadable_intervals=None, unreadable_reasons=None, rhythm_context=None,
            events=None, adjudication_status="not_reviewed", annotation_schema=dict(sample_index="integer",
              beat_presence="certain|uncertain", beat_type="normal_sinus_supported|non_normal_or_unclassified|unknown",
              confidence="reviewer supplied", note="free text")))
        artifacts.append(dict(case_id=case, copied_waveform_sha256=templates[-1]["waveform_sha256"],
                              blind_pngs=7, comparison_pngs=1, review_complete=False))
        print("Rendered %d/%d" % (len(artifacts), len(manifest["clips"])), flush=True)
    dump(OUT / "blind/reference_template.json", templates)
    index = '<!doctype html><meta charset="utf-8"><title>独立参考复核包</title><h1>独立参考复核包</h1><p>先阅读随包说明。未提供任何算法/厂商标记或诊断分组。参考模板中所有判读结果留空。</p><p><a href="reference_template.json">空白参考模板</a></p><ol>'
    index += ''.join('<li><a href="%s/index.html">%s</a></li>' % (c["case_id"], c["case_id"]) for c in manifest["clips"]) + '</ol>'
    (OUT / "blind/index.html").write_text(index, encoding="utf-8")
    dump(OUT / "review_status.json", dict(selection_manifest_sha256=manifest_hash, cases=len(artifacts),
          artifacts=artifacts, independent_reference_received=False, human_reference_completed=0,
          nn_rules_frozen=False, clinical_hrv_released=False, models_trained=False,
          missing_input="Independent competent review of R locations, beat type, and readable intervals",
          producer_script_sha256=sha(Path(__file__).read_bytes())))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        prepare()
    if args.render:
        render()
    if not (args.prepare or args.render):
        parser.error("choose --prepare or --render")
