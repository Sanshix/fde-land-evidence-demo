#!/usr/bin/env python3
"""练习集实测：本机开源 OCR（RapidOCR / PP-OCR ONNX，CPU）+ 条款句式抽取 vs 标准答案。

只在评估时读取标准答案，不把答案输入模型。用法：
  python3 scripts/measure_local_ocr.py [--samples 练习集目录] [--out runs/local-ocr-measure.json]
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.business import SAMPLES  # noqa: E402
from app.domain import FIELDS  # noqa: E402
from app.providers import CloudProvider  # noqa: E402

KEY = ["contract_no", "signed", "tenant", "identity", "parcel_no", "address", "area", "start", "end", "mode",
       "annual", "annual_upper"]
parser = argparse.ArgumentParser()
parser.add_argument("--samples", type=Path, default=SAMPLES)
parser.add_argument("--out", type=Path, default=ROOT / "runs" / "local-ocr-measure.json")
parser.add_argument("--limit", type=int, default=0)
args = parser.parse_args()
truth = {r["档案号"]: r for r in csv.DictReader(open(args.samples / "08_标准答案/合同要素.csv", encoding="utf-8-sig"))}
label = {k: FIELDS[k] for k in KEY}
provider = CloudProvider({"protocol": "local_rapidocr", "provider": "local", "model_id": "PP-OCRv4(RapidOCR ONNX)"})


def norm(key, v):
    t = re.sub(r"\s+", "", str(v or "")).replace("随基准地价调整", "随标准调整")
    if key in {"area", "annual"}:
        try:
            return f"{float(t):.2f}"
        except ValueError:
            return t
    return t


rows, started = [], time.monotonic()
docs = sorted(truth)[: args.limit or None]
for doc in docs:
    pages = sorted((args.samples / "01_合同扫描件").glob(doc + "_*.jpg"))
    t0 = time.monotonic()
    candidates = {k: [] for k in FIELDS}
    for i, page in enumerate(pages):
        out = provider.identify({"id": f"{doc}-{i + 1}", "path": str(page), "source": str(page)})
        for k, v in out["candidates"].items():
            candidates[k] += v
    seconds = time.monotonic() - t0
    row = {"doc": doc, "pages": len(pages), "seconds": round(seconds, 2), "fields": {}}
    for k in KEY:
        expected = norm(k, truth[doc][label[k]])
        values = list(dict.fromkeys(norm(k, c["value"]) for c in candidates[k]))
        row["fields"][k] = {
            "expected": expected,
            "candidates": values,
            "hit": expected in values if expected else not values,
            "unique_correct": values == [expected] if expected else not values,
        }
    row["all_unique_correct"] = all(f["unique_correct"] for f in row["fields"].values())
    rows.append(row)
    print(doc, f"{seconds:.1f}s", sum(f["unique_correct"] for f in row["fields"].values()), "/", len(KEY), flush=True)
summary = {
    "engine": "RapidOCR (PP-OCRv4 ONNX, CPU) + app.extraction 条款句式抽取",
    "documents": len(rows),
    "seconds_total": round(time.monotonic() - started, 1),
    "seconds_per_document": round(sum(r["seconds"] for r in rows) / max(1, len(rows)), 2),
    "field_hit_rate": {FIELDS[k]: round(sum(r["fields"][k]["hit"] for r in rows) / len(rows), 3) for k in KEY},
    "field_unique_correct_rate": {FIELDS[k]: round(sum(r["fields"][k]["unique_correct"] for r in rows) / len(rows), 3) for k in KEY},
    "documents_all_key_fields_unique_correct": sum(r["all_unique_correct"] for r in rows),
}
args.out.parent.mkdir(parents=True, exist_ok=True)
args.out.write_text(json.dumps({"summary": summary, "documents": rows}, ensure_ascii=False, indent=2))
print(json.dumps(summary, ensure_ascii=False, indent=2))
