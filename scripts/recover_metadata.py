"""Validate XML against configJson before recovering missing metadata.

Read-only inputs. Evidence (including names/dates) stays under private/metadata;
public outputs contain aggregate checks only. No diagnoses are auto-confirmed.
"""
import collections
from datetime import datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import zipfile

from audit_ccm import identity_checksum

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "outputs/metadata"
LOCAL = ROOT / "private/metadata"
KEY = (ROOT / "private/pseudonym_key.bin").read_bytes()
FIELDS = ["start", "start_alternate", "duration", "duration_numeric", "age", "sex",
          "report_id", "identity", "source_no", "name", "birth_date"]
IDENTIFIERS = {"report_id", "identity", "source_no"}


def token(kind, value):
    return kind + "_" + hmac.new(KEY, str(value).encode("utf-8"), hashlib.sha256).hexdigest()[:20]


def date_value(value):
    value = value.strip()
    if re.fullmatch(r"\d{2}月\d{2}日\d{4}年", value):
        return datetime.strptime(value, "%m月%d日%Y年").date().isoformat(), "MM月DD日YYYY年"
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).date().isoformat(), fmt
        except ValueError:
            pass
    return datetime.fromisoformat(value).date().isoformat(), "ISO_datetime"


def duration_value(value):
    if re.fullmatch(r"\d+(?:\.\d+)?", value):
        return float(value), "numeric_seconds"
    m = re.fullmatch(r"(?:(\d+)小时)?(?:(\d+)分(?:钟)?)?(?:(\d+)秒)?", value)
    if m and any(x is not None for x in m.groups()):
        h, minute, second = (int(x or 0) for x in m.groups())
        if minute >= 60 or second >= 60:
            raise ValueError("duration_component_out_of_range")
        return h * 3600 + minute * 60 + second, "Chinese_hms"
    raise ValueError("unsupported_duration")


def field(raw, kind, source, config=False, age_unit=None):
    result = dict(raw=raw, value=None, status="absent" if raw is None else "empty", source=source, format=None)
    if raw is None or str(raw).strip() == "":
        return result
    text = str(raw).strip()
    try:
        if kind.startswith("start"):
            val = datetime.fromisoformat(text)
            result.update(value=val.isoformat(sep=" "), format="ISO_datetime")
        elif kind.startswith("duration"):
            val, fmt = duration_value(text)
            if val <= 0:
                raise ValueError("nonpositive_duration")
            result.update(value=val, format=fmt)
        elif kind == "age":
            if config:
                if age_unit != 0:
                    raise ValueError("unmapped_age_unit")
                val = int(text)
                fmt = "config_integer_unit0"
            else:
                m = re.fullmatch(r"(\d+)\s*岁", text)
                if not m:
                    raise ValueError("unsupported_age_unit")
                val, fmt = int(m[1]), "integer_岁"
            if not 1 <= val <= 120:
                raise ValueError("age_requires_review")
            result.update(value=val, format=fmt)
        elif kind == "sex":
            mapping = {"1": "男", "2": "女"} if config else {"男": "男", "女": "女"}
            if text not in mapping:
                raise ValueError("unmapped_sex")
            result.update(value=mapping[text], format="config_code_" + text if config else "Chinese_" + text)
        elif kind == "birth_date":
            val, fmt = date_value(text)
            if int(val[:4]) < 1900:
                raise ValueError("placeholder_birthdate")
            result.update(value=val, format=fmt)
        elif kind in IDENTIFIERS:
            result.update(raw=token(kind, text), value=token(kind, text), format="numeric" if text.isdigit() else "text")
            if kind == "identity":
                result["patient_candidate"] = token("person", identity_checksum(text)) if identity_checksum(text) else None
        else:
            result.update(value=text, format="text")
        result["status"] = "valid"
    except (ValueError, TypeError, OverflowError):
        result["status"] = "unparseable_or_invalid"
    return result


def xml_start(bs, date_tag):
    day, time = bs.get(date_tag), bs.get("RecordStartDateTime")
    source = "BSInfo/" + date_tag + "+BSInfo/RecordStartDateTime"
    if day is None or time is None:
        return field(None, "start", source)
    if not day.strip() or not time.strip():
        return field("", "start", source)
    try:
        date, fmt = date_value(day)
        result = field(date + " " + time, "start", source)
        result.update(raw=dict(date=day, time=time), format=fmt + "+clock")
        return result
    except ValueError:
        return dict(raw=dict(date=day, time=time), value=None, status="unparseable_or_invalid", source=source, format="unrecognized_date")


def parse_xml(raw):
    tree = ET.fromstring(raw)
    bs_node = tree.find("BSInfo")
    if tree.tag != "NewDataSet" or bs_node is None:
        raise ValueError("unsupported_xml_schema")
    bs = {e.tag: e.text or "" for e in bs_node}
    result = {"start": xml_start(bs, "RecordDate"), "start_alternate": xml_start(bs, "RecordStartDate")}
    mapping = {"duration": "RecordTotalCollectTimeSpan", "age": "Age", "sex": "Sex",
               "report_id": "EcgReportId", "identity": "PatientID", "source_no": "SourceNo", "name": "Name", "birth_date": "Birthday"}
    for k, tag in mapping.items():
        result[k] = field(bs.get(tag), k, "BSInfo/" + tag)
    result["duration_numeric"] = field(tree.findtext("AnalysisInfoTotal/OriCollectAllSeconds"), "duration", "AnalysisInfoTotal/OriCollectAllSeconds")
    return result, sorted(bs), tree.tag


def parse_config(detail):
    pt, ex = detail.get("Patient") or {}, detail.get("Examination") or {}
    mapping = {"start": (ex.get("CollectBeginTime"), "Examination/CollectBeginTime"),
               "duration": (ex.get("CollectAllSeconds"), "Examination/CollectAllSeconds"),
               "age": (pt.get("Age"), "Patient/Age+AgeUnit"), "sex": (pt.get("Gender"), "Patient/Gender"),
               "report_id": (detail.get("Id"), "Id"), "identity": (pt.get("IdCard"), "Patient/IdCard"),
               "source_no": (pt.get("SourceNo"), "Patient/SourceNo"), "name": (pt.get("Name"), "Patient/Name"),
               "birth_date": (pt.get("BirthDate"), "Patient/BirthDate")}
    result = {k: field(v, k, source, True, pt.get("AgeUnit")) for k, (v, source) in mapping.items()}
    result["start_alternate"], result["duration_numeric"] = result["start"], result["duration"]
    return result


def consensus(sources, kind):
    entries = [s["fields"][kind] for s in sources if "fields" in s]
    valid = [x for x in entries if x["status"] == "valid"]
    values = {json.dumps(x["value"], ensure_ascii=False, sort_keys=True) for x in valid}
    invalid = any(x["status"] == "unparseable_or_invalid" for x in entries)
    if len(values) > 1:
        return dict(value=None, status="source_conflict")
    if invalid:
        return dict(value=None, status="unparseable_or_invalid")
    if valid:
        result = dict(value=valid[0]["value"], status="valid")
        if kind == "identity":
            result["patient_candidate"] = valid[0].get("patient_candidate")
        return result
    return dict(value=None, status="empty" if any(x["status"] == "empty" for x in entries) else "absent")


def compare(left, right):
    if left["status"] != "valid" or right["status"] != "valid":
        return "not_comparable"
    return "equal" if left["value"] == right["value"] else "different"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    LOCAL.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    audit = [json.loads(line) for line in (ROOT / "outputs/audit/records.jsonl").read_text(encoding="utf-8").splitlines()]
    paths = {x["record_id"]: ROOT / x["relative_path"] for x in json.loads((ROOT / "private/record_paths.json").read_text(encoding="utf-8"))}
    records = []
    for row in audit:
        path = paths[row["record_id"]]
        before = path.stat()
        item = dict(record_id=row["record_id"], record_number=re.search(r"\d+$", path.stem)[0],
                    name=re.sub(r"\d+$", "", path.stem), directory_label=row["category"], confirmed_label=None,
                    config_missing=row["config_count"] == 0, patient_id_prior=row["patient_id"],
                    nb_span_seconds=row["waveform_candidates"][0]["candidate_span_h"] * 3600,
                    sources=[], pdf_members=[], xml_count=0, config_count=0)
        with zipfile.ZipFile(path) as z:
            for index, member in enumerate(z.infolist()):
                ext = Path(member.filename).suffix.lower()
                if ext == ".pdf":
                    item["pdf_members"].append(dict(member_index=index, bytes=member.file_size))
                if ext not in (".xml", ".configjson"):
                    continue
                kind = "xml" if ext == ".xml" else "config"
                item[kind + "_count"] += 1
                source = dict(kind=kind, member_index=index, bytes=member.file_size)
                try:
                    if member.file_size > 20_000_000:
                        raise ValueError("metadata_exceeds_20MB_limit")
                    raw = z.read(member)
                    source.update(sha256=hashlib.sha256(raw).hexdigest(), crc_verified=True)
                    if kind == "xml":
                        fields, schema_keys, root_tag = parse_xml(raw)
                        source.update(fields=fields, schema_keys=schema_keys, root=root_tag)
                        item["sources"].append(source)
                    else:
                        data = json.loads(raw.decode("utf-8-sig"))
                        for node in ("HolterReportDetail", "RealHolterReportDetail"):
                            if isinstance(data.get(node), dict):
                                item["sources"].append(dict(source, report_node=node, fields=parse_config(data[node])))
                except (ValueError, ET.ParseError, UnicodeError, OSError, zipfile.BadZipFile) as exc:
                    item["sources"].append(dict(source, error=type(exc).__name__))
        after = path.stat()
        item["archive_stat_unchanged"] = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        for kind in ("xml", "config"):
            sources = [s for s in item["sources"] if s["kind"] == kind]
            item[kind] = {k: consensus(sources, k) for k in FIELDS}
        item["comparison"] = {k: compare(item["xml"][k], item["config"][k]) for k in FIELDS}
        records.append(item)
        if len(records) % 100 == 0:
            print("Metadata read", len(records), flush=True)
    write_json(LOCAL / "source_evidence.json", records)
    paired = [r for r in records if r["xml_count"] and r["config_count"]]
    validation = {}
    for k in FIELDS:
        counts = collections.Counter(r["comparison"][k] for r in paired)
        comparable = counts["equal"] + counts["different"]
        formats = collections.Counter(s["fields"][k]["format"] for r in paired for s in r["sources"] if s["kind"] == "xml" and "fields" in s)
        validation[k] = dict(paired_records=len(paired), comparable_records=comparable,
                            equal=counts["equal"], different=counts["different"], not_comparable=counts["not_comparable"],
                            agreement_rate=counts["equal"] / comparable if comparable else None,
                            xml_format_counts=dict(formats),
                            validated_mapping=comparable >= 20 and counts["different"] == 0,
                            different_record_ids=[r["record_id"] for r in paired if r["comparison"][k] == "different"])
    # A mapping passes only with >=20 comparable records and no differences.
    # Unvalidated fields stay unresolved; configurations remain references, not truth.
    recovered = []
    for r in records:
        if not r["config_missing"]:
            continue
        dest = {k:r[k] for k in ("record_id", "record_number", "name", "directory_label", "confirmed_label", "nb_span_seconds", "xml_count")}
        dest["pdf_count"] = len(r["pdf_members"])
        dest["fields"] = {}
        for k in FIELDS:
            f = dict(r["xml"][k])
            f["source_refs"] = [dict(member_index=s["member_index"], sha256=s["sha256"],
                                     field_path=s["fields"][k]["source"],
                                     raw_status=s["fields"][k]["status"])
                                for s in r["sources"] if s["kind"] == "xml" and "fields" in s]
            if f["status"] == "valid":
                observed_formats = {s["fields"][k]["format"] for s in r["sources"]
                                    if s["kind"] == "xml" and "fields" in s and s["fields"][k]["status"] == "valid"}
                supported_formats = set(validation[k]["xml_format_counts"])
                if not observed_formats.issubset(supported_formats):
                    f["status"] = "unvalidated_xml_field_format"
                elif not validation[k]["validated_mapping"]:
                    f["status"] = "mapping_not_validated"
                else:
                    f["status"] = "recovered_from_validated_xml_mapping"
            dest["fields"][k] = f
        for primary, secondary in (("start", "start_alternate"), ("duration", "duration_numeric")):
            a, b = dest["fields"][primary], dest["fields"][secondary]
            if a["value"] is not None and b["value"] is not None and a["value"] != b["value"]:
                a["status"] = b["status"] = "xml_internal_conflict"
        span = dest["fields"]["duration"]
        if span["value"] is not None and abs(span["value"] - r["nb_span_seconds"]) > 1:
            span["status"] = "xml_nb_duration_conflict"
        report = dest["fields"]["report_id"]
        if report["value"] is not None and report["value"] != token("report_id", r["record_number"]):
            report["status"] = "xml_filename_record_conflict"
            for f in dest["fields"].values():
                if f["status"] == "recovered_from_validated_xml_mapping":
                    f["status"] = "record_link_requires_review"
        dest["timezone"] = None
        dest["record_link_status"] = ("xml_report_id_matches_filename" if report["status"] == "recovered_from_validated_xml_mapping"
                                      else "xml_inside_ccm_name_and_duration_match_report_id_missing")
        if dest["fields"]["name"]["value"] is not None and dest["fields"]["name"]["value"] != r["name"]:
            dest["record_link_status"] = "xml_filename_name_conflict"
            for f in dest["fields"].values():
                if f["status"] == "recovered_from_validated_xml_mapping":
                    f["status"] = "record_link_requires_review"
        if not r["xml_count"]:
            dest["record_link_status"] = "xml_absent"
        dest["timezone_status"] = "not_encoded_in_sources"
        dest["patient_research_id_confirmed"] = None
        dest["patient_id_candidate"] = dest["fields"]["identity"].get("patient_candidate") if dest["fields"]["identity"]["status"] == "recovered_from_validated_xml_mapping" else None
        recovered.append(dest)
    write_json(LOCAL / "recovered_metadata.json", recovered)
    summary = dict(total_records=len(records), missing_config_records=len(recovered), paired_records=len(paired),
                   missing_config_with_xml=sum(bool(r["xml_count"]) for r in recovered),
                   missing_config_with_pdf=sum(bool(r["pdf_count"]) for r in recovered),
                   missing_config_with_neither=sum(not r["xml_count"] and not r["pdf_count"] for r in recovered),
                   source_errors=sum(bool(s.get("error")) for r in records for s in r["sources"]),
                   all_archive_stats_unchanged=all(r["archive_stat_unchanged"] for r in records),
                   validation=validation,
                   missing_xml_field_status={k:dict(collections.Counter(r["xml"][k]["status"] for r in records if r["config_missing"])) for k in FIELDS},
                   recovered_field_status={k:dict(collections.Counter(r["fields"][k]["status"] for r in recovered)) for k in FIELDS})
    write_json(OUT / "validation_summary.json", summary)
    print(json.dumps({k:v for k,v in summary.items() if k not in ("validation", "missing_xml_field_status", "recovered_field_status")}, indent=2))
    for k, v in validation.items():
        print(k, "equal", v["equal"], "different", v["different"], "not_comparable", v["not_comparable"], "mapping_pass", v["validated_mapping"])


if __name__ == "__main__":
    main()
