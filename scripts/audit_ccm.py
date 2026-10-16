"""Read-only CCM inventory. No waveform decoding, clinical labels, or HRV claims.

Run from the project root: python scripts/audit_ccm.py
Only reads ZIP directories, small configs and binary prefixes. Full archive SHA256
and full member CRC checks are deliberately reported as NOT performed.
"""
import collections
import hashlib
import hmac
import json
import pathlib
import re
import secrets
import struct
import sys
from datetime import datetime, timezone
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs" / "audit"
PRIVATE = ROOT / "private"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def identity_checksum(value):
    """Format/checksum only; does NOT authenticate a person or registry entry."""
    value = str(value or "").strip().upper()
    if not re.fullmatch(r"[1-9][0-9]{16}[0-9X]", value):
        return None
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    if "10X98765432"[sum(int(a) * b for a, b in zip(value[:17], weights)) % 11] != value[-1]:
        return None
    try:
        datetime.strptime(value[6:14], "%Y%m%d")
    except ValueError:
        return None
    return value


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    PRIVATE.mkdir(exist_ok=True)
    key_path = PRIVATE / "pseudonym_key.bin"
    if not key_path.exists():
        key_path.write_bytes(secrets.token_bytes(32))
    key = key_path.read_bytes()

    def token(kind, text):
        return kind + "_" + hmac.new(key, str(text).encode("utf-8"), hashlib.sha256).hexdigest()[:20]

    records, mapping, config_rows = [], [], []
    for path in sorted((ROOT / "data").rglob("*.ccm")):
        rel = path.relative_to(ROOT).as_posix()
        rid = token("r", rel)
        name_hint = re.sub(r"\d+$", "", path.stem).strip()
        row = dict(record_id=rid, category=path.parent.name, archive_bytes=path.stat().st_size,
                   filename_name_hint=token("name", name_hint), errors=[], waveform_candidates=[],
                   beat_headers=[], config_count=0, full_sha256=None, full_crc_verified=False,
                   patient_id=None, identity_status="unresolved")
        mapping.append(dict(record_id=rid, relative_path=rel))
        candidates = []
        try:
            with zipfile.ZipFile(path) as z:
                infos = z.infolist()
                row["zip_directory_readable"] = True
                row["member_count"] = len(infos)
                row["member_extensions"] = dict(collections.Counter(pathlib.Path(i.filename).suffix.lower() for i in infos))
                row["duplicate_member_names"] = len(infos) != len(set(i.filename for i in infos))
                row["encrypted_members"] = sum(bool(i.flag_bits & 1) for i in infos)
                for index, info in enumerate(infos):
                    ext = pathlib.Path(info.filename).suffix.lower()
                    ref = "member_%02d" % index
                    if ext == ".nb":
                        with z.open(info) as f:
                            header = f.read(12)
                        w = dict(member_ref=ref, bytes=info.file_size, directory_crc32=info.CRC,
                                 header_hex=header.hex(), layout_status="unresolved")
                        if len(header) == 12:
                            ver, fs, frames = struct.unpack("<III", header)
                            payload = info.file_size - 12
                            w.update(header_word0=ver, candidate_fs_hz=fs, candidate_frames=frames)
                            if 1 <= fs <= 10000 and frames > 0 and payload % frames == 0:
                                width = payload // frames
                                w.update(bytes_per_frame=width, candidate_span_h=frames / fs / 3600,
                                         candidate_int16_channels=width // 2 if width % 2 == 0 else None,
                                         layout_status="length_consistent_hypothesis")
                        row["waveform_candidates"].append(w)
                    elif ext in (".ba", ".bd"):
                        with z.open(info) as f:
                            header = f.read(20)
                        row["beat_headers"].append(dict(member_ref=ref, extension=ext, bytes=info.file_size,
                                                        prefix_hex=header.hex(), semantics="unverified"))
                    elif ext == ".configjson":
                        row["config_count"] += 1
                        if info.file_size > 10_000_000:
                            row["errors"].append(ref + ":config_exceeds_10MB_limit")
                            continue
                        try:
                            raw = z.read(info)  # full small member read verifies this member's CRC
                            config = json.loads(raw.decode("utf-8-sig"))
                            for root_name in ("HolterReportDetail", "RealHolterReportDetail"):
                                detail = config.get(root_name)
                                if not isinstance(detail, dict):
                                    continue
                                pt = detail.get("Patient") or {}
                                ex = detail.get("Examination") or {}
                                device = ex.get("DeviceDetail") or {}
                                ids = set(filter(None, (identity_checksum(pt.get(k)) for k in ("IdCard", "IdentityNo"))))
                                start_raw = ex.get("CollectBeginTime")
                                try:
                                    start = datetime.fromisoformat(start_raw)
                                except (TypeError, ValueError):
                                    start = None
                                c = dict(record_id=rid, config_ref=ref, report_node=root_name,
                                         config_sha256=hashlib.sha256(raw).hexdigest(),
                                         age_raw=pt.get("Age"), age_unit_raw=pt.get("AgeUnit"),
                                         gender_code_raw=pt.get("Gender"),
                                         candidate_identity_tokens=sorted(token("person", v) for v in ids),
                                         patient_name_hint=token("name", str(pt.get("Name") or "").strip()) if pt.get("Name") else None,
                                         birth_hint=token("birth", pt["BirthDate"]) if pt.get("BirthDate") else None,
                                         start_parseable=start is not None,
                                         start_clock=start.strftime("%H:%M:%S") if start else None,
                                         start_time_hint=token("time", start_raw) if start_raw else None,
                                         start_has_timezone=bool(start and start.tzinfo),
                                         declared_seconds=ex.get("CollectAllSeconds"),
                                         brand=device.get("DeviceBrand"), model=device.get("DeviceModelNumber"),
                                         declared_leads=device.get("LeadNum"), device_version=device.get("DeviceVersion"),
                                         medication_nonempty=bool(pt.get("Medication")),
                                         past_history_nonempty=bool(pt.get("PastHistory")))
                                config_rows.append(c)
                                candidates.append(c)
                        except (ValueError, UnicodeError, OSError, RuntimeError, zipfile.BadZipFile, AttributeError) as exc:
                            row["errors"].append(ref + ":" + type(exc).__name__)
                # This is a duplicate screening signature, NOT a content hash.
                nb_keys = sorted((w["bytes"], w["directory_crc32"]) for w in row["waveform_candidates"])
                row["waveform_crc_screening_key"] = hashlib.sha256(json.dumps(nb_keys).encode()).hexdigest() if nb_keys else None
        except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
            row["errors"].append(type(exc).__name__)
        identity_sets = [tuple(c["candidate_identity_tokens"]) for c in candidates]
        union = set(v for ids in identity_sets for v in ids)
        if len(union) == 1 and identity_sets and all(len(ids) == 1 for ids in identity_sets):
            row["patient_id"] = next(iter(union))
            row["identity_status"] = "provisional_checksum_valid_identifier_not_clinically_confirmed"
        elif union:
            row["identity_status"] = "identifier_conflict_or_missing_across_configs"
        row["config_disagreements"] = [field for field in
            ("age_raw", "age_unit_raw", "gender_code_raw", "patient_name_hint", "birth_hint", "start_time_hint",
             "declared_seconds", "brand", "model", "declared_leads", "candidate_identity_tokens")
            if len({json.dumps(c[field], sort_keys=True) for c in candidates}) > 1]
        row["config_consensus"] = {field: candidates[0][field] for field in
            ("age_raw", "age_unit_raw", "gender_code_raw", "start_clock", "declared_seconds", "brand", "model", "declared_leads")
            if candidates and len({json.dumps(c[field]) for c in candidates}) == 1}
        records.append(row)
        if len(records) % 100 == 0:
            print("Audited", len(records), "archives", flush=True)

    def grouped(field, minimum=2):
        groups = collections.defaultdict(list)
        for r in records:
            if r.get(field):
                groups[r[field]].append(r)
        return [dict(group_id=k, record_ids=[r["record_id"] for r in rs],
                     categories=sorted(set(r["category"] for r in rs)), record_count=len(rs))
                for k, rs in sorted(groups.items()) if len(rs) >= minimum]

    repeated_ids = grouped("patient_id")
    name_groups = grouped("filename_name_hint")
    crc_groups = grouped("waveform_crc_screening_key")
    patients = grouped("patient_id", 1)
    summary = dict(audit_utc=datetime.now(timezone.utc).isoformat(), records=len(records),
                   total_archive_bytes=sum(r["archive_bytes"] for r in records),
                   category_counts=dict(collections.Counter(r["category"] for r in records)),
                   readable_zip_directories=sum(r.get("zip_directory_readable", False) for r in records),
                   records_with_errors=sum(bool(r["errors"]) for r in records),
                   provisional_identity_groups=len(patients),
                   unresolved_identity_records=sum(r["patient_id"] is None for r in records),
                   repeated_identifier_groups=len(repeated_ids),
                   cross_category_identifier_groups=sum(len(g["categories"]) > 1 for g in repeated_ids),
                   repeated_filename_name_groups=len(name_groups),
                   cross_category_filename_name_groups=sum(len(g["categories"]) > 1 for g in name_groups),
                   waveform_crc_candidate_groups=len(crc_groups),
                   records_with_config_disagreements=sum(bool(r["config_disagreements"]) for r in records),
                   limits=["No full archive SHA256 or full ZIP CRC verification", "ZIP length is not valid ECG coverage",
                           "Patient IDs are provisional metadata matches, not verified identities",
                           "Beat semantics, lead mapping, gain, timezone and NN validity remain unverified"])
    for name, rows in (("records", records), ("config_evidence", config_rows)):
        with (OUT / (name + ".jsonl")).open("w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
    for name, obj in (("summary", summary), ("patient_candidates", patients),
                      ("repeated_identifiers", repeated_ids), ("repeated_name_hints", name_groups),
                      ("waveform_duplicate_candidates", crc_groups)):
        write_json(OUT / (name + ".json"), obj)
    write_json(PRIVATE / "record_paths.json", mapping)
    print(json.dumps(summary, ensure_ascii=True, indent=2))


def verify_duplicate_candidates():
    """Hash screened candidates only; not a full-dataset integrity check."""
    mapping = {r["record_id"]: ROOT / r["relative_path"] for r in
               json.loads((PRIVATE / "record_paths.json").read_text(encoding="utf-8"))}
    results = []

    def digest(stream):
        h = hashlib.sha256()
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(chunk)
        return h.hexdigest()

    for group in json.loads((OUT / "waveform_duplicate_candidates.json").read_text(encoding="utf-8")):
        for rid in group["record_ids"]:
            path = mapping[rid]
            with path.open("rb") as f:
                row = dict(record_id=rid, archive_sha256=digest(f))
            with zipfile.ZipFile(path) as z:
                waves = [i for i in z.infolist() if i.filename.lower().endswith(".nb")]
                if len(waves) != 1:
                    raise ValueError("Duplicate verification requires exactly one NB member")
                with z.open(waves[0]) as f:
                    row.update(nb_sha256=digest(f), nb_crc_verified=True)
            results.append(row)
    write_json(OUT / "duplicate_hash_verification.json", results)
    print("Fully hashed", len(results), "candidate archives and their NB members")


if __name__ == "__main__":
    if "--verify-duplicates" in sys.argv:
        verify_duplicate_candidates()
    else:
        main()
