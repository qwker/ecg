"""Add the offline manual annotator using existing NPZ only; no CCM or plots."""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
BLIND = ROOT / "outputs/reference_review/blind"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    refs = json.loads((BLIND / "reference_template.json").read_text(encoding="utf-8"))
    original_hashes = {"reference_template.json": sha(BLIND / "reference_template.json")}
    generated = BLIND / "waveforms"
    generated.mkdir(exist_ok=True)
    payloads = []
    for ref in refs:
        source = BLIND / ref["waveform_file"]
        digest = sha(source)
        assert digest == ref["waveform_sha256"]
        with np.load(source, allow_pickle=False) as data:
            wave = data["raw_counts"]
            assert wave.ndim == 2 and wave.shape[1] == 8 and wave.dtype.kind == "i" and wave.dtype.itemsize == 2
            raw = wave.astype("<i2", copy=False).tobytes(order="C")
            payload = dict(case_id=ref["case_id"], fs=int(data["fs"]), start_sample=int(data["start_sample"]), frames=len(wave),
                           waveform_sha256=digest, raw_payload_sha256=hashlib.sha256(raw).hexdigest(), base64=base64.b64encode(raw).decode("ascii"))
        dest = generated / (ref["case_id"] + ".js")
        dest.write_text('window.REVIEW_WAVEFORMS[' + json.dumps(ref["case_id"]) + '] = ' + json.dumps(payload, separators=(",", ":")) + ';\n', encoding="utf-8")
        payloads.append(dict(case_id=ref["case_id"], original_npz_sha256=digest, raw_payload_sha256=payload["raw_payload_sha256"], generated_js_sha256=sha(dest)))
        original_hashes[ref["waveform_file"]] = digest
    (BLIND / "cases.js").write_text("window.REVIEW_CASES = " + json.dumps(refs, ensure_ascii=False, separators=(",", ":")) + ";\n", encoding="utf-8")
    for file in ["annotate.html", "annotator.js", "annotator.css"]:
        shutil.copyfile(ROOT / "review_app" / file, BLIND / file)
    index = BLIND / "index.html"
    backup = BLIND / "index.static.html"
    if not backup.exists():
        shutil.copyfile(index, backup)
    original = backup.read_text(encoding="utf-8")
    banner = '<p style="padding:16px;background:#dff2ed;font:18px sans-serif"><a href="annotate.html">打开交互标注工具：缩放、点击、撤销、区间与保存</a></p>'
    # Preserve the original static index as a separately accessible file.
    index.write_text(original.replace("<h1>", banner + "<h1>", 1), encoding="utf-8")
    assert all(sha(BLIND / name) == digest for name, digest in original_hashes.items())
    manifest = dict(tool_version="manual-review-1.0.0", cases=len(refs), input="existing NPZ and reference template only",
                    original_files_unchanged=True, originals_sha256=original_hashes, payloads=payloads,
                    app_sha256={name: sha(BLIND / name) for name in ["annotate.html", "annotator.js", "annotator.css", "cases.js"]},
                    builder_sha256=sha(Path(__file__)))
    (ROOT / "outputs/reference_review/annotator_build.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print("Built offline annotator for %d cases; original NPZ/template unchanged; no CCM read or PNG generated." % len(refs))


if __name__ == "__main__":
    main()
