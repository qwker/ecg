"""Finalize local review tables; run using bundled Python with pdfplumber."""
import collections
import hashlib
import json
from pathlib import Path
import re

import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "private/metadata"
OUT = ROOT / "outputs/metadata"


def main():
    recovered = json.loads((LOCAL / "recovered_metadata.json").read_text(encoding="utf-8"))
    evidence = json.loads((LOCAL / "source_evidence.json").read_text(encoding="utf-8"))
    by_id = {r["record_id"]: r for r in recovered}
    pdf_reviews = []
    for path in sorted((LOCAL / "pdf_review").glob("*.pdf")):
        with pdfplumber.open(path) as pdf:
            page = pdf.pages[0]
            text = page.crop((0, 0, page.width, page.height * .20)).extract_text() or ""
            def find(pattern):
                match = re.search(pattern, text)
                return match[1] if match else None
            age = find(r"年龄[：:]\s*(\d+)\s*岁")
            sex = find(r"性别[：:]\s*([男女])")
            start = find(r"记录日期[：:]\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})")
            review = dict(record_id=path.stem, pdf_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                          pages=len(pdf.pages), source="embedded PDF, first-page patient-information header",
                          header_text=text, age_years=int(age) if age else None, sex=sex, record_start=start,
                          outpatient_id_candidate=find(r"门诊号[：:]\s*([0-9]+)"),
                          visual_header_review=True, full_text_extracted=True,
                          stable_patient_id_verified=False)
            record = by_id[path.stem]
            review["comparisons"] = {key: ("both_missing" if value is None and record["fields"][key]["value"] is None
                                            else "equal" if value == record["fields"][key]["value"] else "different_or_unreadable")
                                      for key, value in (("age", review["age_years"]), ("sex", sex), ("start", start))}
            review["recovery_decision"] = "No new verified age or stable patient identifier; outpatient number is only an unverified candidate."
            pdf_reviews.append(review)
    (LOCAL / "pdf_review/review_summary.json").write_text(json.dumps(pdf_reviews, ensure_ascii=False, indent=2), encoding="utf-8")
    pdf_by_id = {r["record_id"]: r for r in pdf_reviews}
    for r in recovered:
        r["pdf_review"] = pdf_by_id.get(r["record_id"])
        r["required_follow_up"] = ["确认患者研究编号", "确认诊断标签", "确认采集时间所用时区"]
        for field, label in (("start", "补采集开始日期时间"), ("age", "补年龄"), ("sex", "补性别")):
            if r["fields"][field]["status"] != "recovered_from_validated_xml_mapping":
                r["required_follow_up"].append(label)
        if r["fields"]["report_id"]["value"] is None and r["xml_count"]:
            r["required_follow_up"].append("XML无报告编号，核对该次检查")
        if r["patient_id_candidate"] and any(e["patient_id_prior"] == r["patient_id_candidate"] for e in evidence):
            r["required_follow_up"].append("暂定身份匹配既有记录，核查重复检查")
    (LOCAL / "metadata_table.json").write_text(json.dumps(recovered, ensure_ascii=False, indent=2), encoding="utf-8")
    checks = dict(records=len(recovered), xml_records=sum(bool(r["xml_count"]) for r in recovered), pdf_reviews=len(pdf_reviews),
                  age_missing_names=[r["name"] for r in recovered if r["xml_count"] and r["fields"]["age"]["value"] is None],
                  pdf_comparison_counts={key:dict(collections.Counter(p["comparisons"][key] for p in pdf_reviews)) for key in ("age", "sex", "start")},
                  new_verified_pdf_fields=0,
                  clinical_labels_confirmed=0,
                  confirmed_patient_research_ids=0)
    (OUT / "completion_checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding="utf-8")
    assert len(recovered) == 66 and len({r["record_id"] for r in recovered}) == 66
    assert checks["xml_records"] == 26 and len(pdf_reviews) == 8
    assert all(r["confirmed_label"] is None and r["patient_research_id_confirmed"] is None for r in recovered)
    assert all(r["fields"]["start"]["source_refs"] for r in recovered if r["xml_count"])
    print(json.dumps(checks, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
