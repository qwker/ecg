"""Build 27 provisional format-review cases; never label vendor events as NN.

Uses existing numpy/matplotlib. Run after audit_ccm.py. Binary layouts below are
explicit hypotheses; generated images are review material, not validation truth.
"""
import collections
import hashlib
import json
import pathlib
import struct
import zipfile

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "probe"


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    rows = [json.loads(s) for s in (ROOT / "outputs/audit/records.jsonl").read_text(encoding="utf-8").splitlines()]
    paths = {x["record_id"]: ROOT / x["relative_path"] for x in json.loads((ROOT / "private/record_paths.json").read_text(encoding="utf-8"))}
    groups = collections.defaultdict(list)
    for r in rows:
        groups[r["category"]].append(r)
    duplicates = {rid for g in json.loads((ROOT / "outputs/audit/waveform_duplicate_candidates.json").read_text(encoding="utf-8")) for rid in g["record_ids"]}
    selected = []
    for category, rs in sorted(groups.items()):
        rs = sorted(rs, key=lambda r: (r["waveform_candidates"][0].get("candidate_span_h", 0), r["record_id"]))
        picks = [(rs[0], "shortest_header_span")]
        priority = sorted((r for r in rs if r != rs[0]), key=lambda r: (r["record_id"] not in duplicates, r["config_count"] != 0, r["record_id"]))
        picks.append((priority[0], "duplicate_candidate_or_missing_config_or_id_order"))
        remaining = [r for r in rs if r not in [x[0] for x in picks]]
        picks.append((max(remaining, key=lambda r: (r["config_count"], r["record_id"])), "most_config_versions"))
        selected.extend(picks)
    evidence = []
    for r, selection_reason in selected:
        rid = r["record_id"]
        item = dict(record_id=rid, category=r["category"], selection_reason=selection_reason,
                    normal_beat_code=None, manually_validated=False, clinical_hrv_eligible=False)
        with zipfile.ZipFile(paths[rid]) as z:
            infos = z.infolist()
            nb = [i for i in infos if i.filename.lower().endswith(".nb")]
            ba = [i for i in infos if i.filename.lower().endswith(".ba")]
            bd = [i for i in infos if i.filename.lower().endswith(".bd")]
            if len(nb) != 1 or len(ba) != 1 or len(bd) != 1:
                item["error"] = "nonunique_or_missing_binary_member"
                evidence.append(item)
                continue
            arrays = {}
            for kind, info in (("ba", ba[0]), ("bd", bd[0])):
                raw = z.read(info)
                version = struct.unpack_from("<I", raw, 0)[0]
                stride, count = struct.unpack_from("<II", raw, 14)
                expected_stride = 44 if kind == "ba" else 48
                matches = stride == expected_stride and len(raw) == 4096 + count * stride
                item[kind + "_structure"] = dict(version=version, stride_at14=stride, count_at18=count,
                    header_bytes_hypothesis=4096, size_matches=matches, member_crc_verified=True,
                    sha256=hashlib.sha256(raw).hexdigest())
                if matches:
                    arrays[kind] = np.frombuffer(raw, dtype="<i4", offset=4096).reshape(count, stride // 4)
            with z.open(nb[0]) as f:
                h = f.read(12)
                version, fs, frames = struct.unpack("<III", h)
                if fs != 250 or nb[0].file_size != 12 + frames * 16:
                    item["error"] = "unsupported_waveform_hypothesis"
                    evidence.append(item)
                    continue
                raw_wave = f.read(min(frames, fs * 330) * 16)
            wave = np.frombuffer(raw_wave, dtype="<i2").reshape(-1, 8)
            item["waveform_prefix_seconds"] = len(wave) / fs
            item["waveform_prefix_sha256"] = hashlib.sha256(h + raw_wave).hexdigest()
            item["waveform_hypothesis"] = "12-byte header, little-endian signed int16, frame-interleaved 8 channels; gain/lead map unverified"
            if "ba" in arrays:
                events = arrays["ba"][:, 4].astype(np.int64)
                diff = np.diff(events)
                item["ba_column4_candidate"] = dict(count=len(events), first=int(events[0]), last=int(events[-1]),
                    nonpositive_differences=int(np.sum(diff <= 0)), out_of_range=int(np.sum((events < 0) | (events >= frames))),
                    interval_ms_percentiles=np.percentile(diff * 1000 / fs, [0, 1, 50, 99, 100]).tolist())
            else:
                events = np.array([], dtype=np.int64)
            if "bd" in arrays:
                codes, counts = np.unique(arrays["bd"][:, 0], return_counts=True)
                item["bd_column0_raw_counts"] = {str(k): int(v) for k, v in zip(codes, counts)}
            item["ba_bd_counts_match"] = (len(arrays["ba"]) == len(arrays["bd"])) if len(arrays) == 2 else None
            # Two early windows only; deliberately no inference about 24h coverage.
            item["review_windows"] = []
            fig, axes = plt.subplots(8, 2, figsize=(15, 11), sharex="col")
            for col, start in enumerate((60, 300)):
                lo, hi = start * fs, (start + 10) * fs
                segment = wave[lo:hi]
                chosen = events[(events >= lo) & (events < hi)]
                item["review_windows"].append(dict(offset_seconds=start, duration_seconds=10,
                    candidate_event_indices=chosen.tolist(), human_reference_events=None,
                    human_normal_beat_labels=None, all_channels_zero_fraction=float(np.mean(np.all(segment == 0, axis=1))),
                    status="awaiting_independent_annotation"))
                for ch in range(8):
                    ax = axes[ch, col]
                    ax.plot(np.arange(len(segment)) / fs + start, segment[:, ch], lw=0.7, color="#24496a")
                    for event in chosen:
                        ax.axvline(event / fs, color="#c4552c", alpha=0.4, lw=0.6)
                    ax.set_ylabel("Ch%d\nraw count" % (ch + 1), fontsize=8)
                    ax.grid(alpha=0.15)
                    if ch == 0:
                        ax.set_title("Offset %d-%d s" % (start, start + 10), fontsize=10)
                    if ch == 7:
                        ax.set_xlabel("Seconds from recording start")
            fig.suptitle(rid + " | PROVISIONAL decoding\nOrange = BA column 4 hypothesis; no lead names, gain or normal-beat codes verified", fontsize=11)
            fig.tight_layout(rect=(0, 0, 1, 0.95))
            filename = rid + ".png"
            fig.savefig(OUT / filename, dpi=120)
            plt.close(fig)
            item["review_image"] = filename
        evidence.append(item)
        print("Probed", len(evidence), "/", len(selected), flush=True)
    (OUT / "evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Saved", len(evidence), "cases. Clinical NN/HRV validation NOT completed.")


if __name__ == "__main__":
    main()
