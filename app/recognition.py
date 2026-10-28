"""Evidence-preserving local cascade. Confidence is only one review trigger."""

import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

from PIL import Image, ImageFilter, ImageStat

from .domain import FIELDS, clean

ROOT = Path(__file__).resolve().parents[1]
PARSER_VERSION = "spatial-fields-v1"
CONTEXT_PROMPT_VERSION = "evidence-candidates-v1"
CRITICAL = {"tenant", "identity", "parcel_no", "area", "start", "end", "annual", "mode"}
LABELS = {
    "tenant": ["承租人"],
    "identity": ["证件号码", "证件号"],
    "phone": ["联系电话"],
    "parcel_no": ["地号", "宗地号"],
    "address": ["坐落"],
    "area": ["土地面积"],
    "use": ["土地用途", "用途"],
    "grade": ["土地级别", "士地级别"],
    "annual": ["年租金（小写）", "年租金(小写)", "年租金人民币"],
    "annual_upper": ["年租金（大写）", "年租金(大写)"],
    "contract_no": ["合同编号"],
}


def quality(path):
    with Image.open(path) as image:
        gray = image.convert("L")
        reduced = gray.copy()
        reduced.thumbnail((1200, 1200))
        contrast = ImageStat.Stat(reduced).stddev[0]
        sharpness = ImageStat.Stat(reduced.filter(ImageFilter.FIND_EDGES)).stddev[0]
        return {
            "width": image.width,
            "height": image.height,
            "contrast": round(contrast, 2),
            "edge_std": round(sharpness, 2),
            "warnings": (["图像对比度偏低"] if contrast < 20 else [])
            + (["边缘细节较弱，需看原图"] if sharpness < 12 else []),
        }


def extract(lines, page_id, model):
    candidates = {k: [] for k in FIELDS}

    def add(key, value, line, reason="原文识别"):
        value = clean(value).strip("：:;,，；。")
        if not value or len(value) > 160:
            return
        candidate = {
            "value": value,
            "raw_text": line["text"],
            "model": model,
            "source_page_id": page_id,
            "bbox": line["box"],
            "score": line["score"],
            "kind": "ocr",
            "reason": reason,
            "verified": False,
        }
        if not any(c["value"] == value and c["source_page_id"] == page_id for c in candidates[key]):
            candidates[key].append(candidate)

    for line in lines:
        t = line["text"]
        dates = re.findall(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?", t)
        if len(dates) >= 2:
            for key, ds in zip(("start", "end"), dates[:2], strict=True):
                add(key, f"{int(ds[0]):04}-{int(ds[1]):02}-{int(ds[2]):02}", line)
        for key, labels in LABELS.items():
            matched = next((label for label in labels if label in t), None)
            if not matched:
                continue
            value_line = line
            value = t.split(matched, 1)[1].strip("：: ")
            value = re.sub(r"^（[甲乙]方）|^\([甲乙]方\)", "", value).strip()
            if not value or (key == "area" and not re.search(r"\d{2,}", value)):
                x1, y1, x2, y2 = line["box"]
                right = [
                    item
                    for item in lines
                    if item is not line
                    and item["box"][0] >= x2 - 8
                    and abs((item["box"][1] + item["box"][3] - y1 - y2) / 2) < max(18, (y2 - y1) * 0.85)
                ]
                if not right:
                    continue
                value_line = min(right, key=lambda item: item["box"][0])
                value = value_line["text"]
            if key in {"area", "annual"}:
                for n in re.findall(r"\d[\d,]*\.\d{1,2}|\d{2,}", value):
                    add(key, n.replace(",", ""), value_line)
            elif key == "annual_upper":
                match = re.search(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿元角分整正]+", value)
                if match:
                    add(key, match[0], value_line)
            elif key == "use":
                for use in ("住宅", "商服", "工业", "仓储"):
                    if use in value:
                        add(key, use, value_line)
            elif key == "grade":
                match = re.search(r"[一二三四]级", value)
                if match:
                    add(key, match[0], value_line)
            elif key == "phone":
                for n in re.findall(r"\d{7,12}", value):
                    add(key, n, value_line)
            elif key == "identity":
                match = re.search(r"[0-9A-Za-z]{15,20}", value)
                if match:
                    add(key, match[0], value_line)
            else:
                add(key, value, value_line)
        # Never infer a checkbox from presence of its label alone.
        if re.search(r"[☑✓√]\s*固定", t):
            add("mode", "固定", line, "检测到勾选字符，仍需核对原图")
        if re.search(r"[☑✓√]\s*随", t):
            add("mode", "随标准调整", line, "检测到勾选字符，仍需核对原图")
    return candidates


def assess(candidates, qualities=()):
    risks = []
    for key in CRITICAL:
        values = {c["value"] for c in candidates.get(key, [])}
        if not values:
            risks.append({"field": key, "code": "missing", "reason": f"{FIELDS[key]}缺少可引用候选"})
        if len(values) > 1:
            risks.append(
                {"field": key, "code": "disagreement", "reason": f"{FIELDS[key]}存在跨模型/跨页/原文多值"}
            )
        if any(c.get("score", 0) < 0.9 for c in candidates.get(key, [])):
            risks.append({"field": key, "code": "low_score", "reason": "识别分数偏低，不能据此判定正确"})
    for key, cs in candidates.items():
        for c in cs:
            if not (c.get("bbox") or c.get("source_locator")) or not c.get("raw_text"):
                risks.append({"field": key, "code": "no_evidence", "reason": "缺少原文区域"})
            if key == "phone" and len(c["value"]) != 11:
                risks.append({"field": key, "code": "invalid_format", "reason": "联系电话不是11位"})
    for key, cs in candidates.items():
        for c in cs:
            try:
                if key in {"start", "end", "signed"}:
                    date.fromisoformat(c["value"])
                elif key in {"area", "annual"}:
                    value = Decimal(c["value"])
                    if not value.is_finite() or value < 0 or (key == "area" and value == 0):
                        raise ValueError()
                elif key == "mode" and c["value"] not in {"固定", "随标准调整"}:
                    raise ValueError()
                elif key == "use" and c["value"] not in {"住宅", "商服", "工业", "仓储"}:
                    raise ValueError()
            except Exception:
                risks.append(
                    {
                        "field": key,
                        "code": "invalid_format",
                        "reason": f"{FIELDS[key]}未通过格式/业务取值校验",
                    }
                )
    starts = {c["value"] for c in candidates.get("start", [])}
    ends = {c["value"] for c in candidates.get("end", [])}
    if starts and ends and min(starts) > max(ends):
        risks.append({"field": "start", "code": "cross_field_conflict", "reason": "起始日晚于所有终止日候选"})
    risks += [{"field": None, "code": "quality", "reason": w} for q in qualities for w in q["warnings"]]
    # All records require a human visual check for crossing-out and checkbox ambiguity.
    risks.append(
        {
            "field": None,
            "code": "visual_review",
            "reason": "OCR一致也可能共同出错：须核对涂改、签章、勾选与缺页",
        }
    )
    return risks


def context_candidate(payload):
    """Validate shape only, never promote contextual reasoning to source truth."""
    if payload.get("status") == "cannot_determine":
        return {"status": "cannot_determine", "reason": str(payload.get("reason", "证据不足"))[:500]}
    if not payload.get("citations") or not payload.get("reason"):
        raise ValueError("上下文候选必须有可核对理由和页/区域引用")
    if payload.get("field") not in FIELDS:
        raise ValueError("未知字段")
    return {
        **payload,
        "kind": "context_suggestion",
        "verified": False,
        "can_publish": False,
        "warning": "上下文合理不代表原文如此；无原件证据须挂起，不得靠合算补事实",
    }


def run_cascade(pages, run_dir, strategy="cascade", budget=180):
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    out = {
        "mode": "live-local",
        "parser_version": PARSER_VERSION,
        "context_prompt_version": CONTEXT_PROMPT_VERSION,
        "strategy": strategy,
        "pages": [],
        "candidates": {k: [] for k in FIELDS},
        "events": [],
        "third_model": "not_configured",
        "context_model": "not_configured",
        "external_calls": 0,
        "api_cost": "0.00",
        "cost_note": "仅外部API费用；未估算本机电力/硬件",
        "started_at": time.time(),
    }
    spec = {"paths": [p["path"] for p in pages], "output": str(run_dir / "paddle.json")}
    (run_dir / "spec.json").write_text(json.dumps(spec))
    try:
        with (run_dir / "paddle.log").open("w") as log:
            process = subprocess.run(
                [sys.executable, str(ROOT / "scripts/paddle_worker.py"), str(run_dir / "spec.json")],
                stdout=log,
                stderr=log,
                timeout=min(budget * 0.65, 120),
            )
        if process.returncode:
            raise RuntimeError("Paddle进程失败，详见本地run日志")
    except Exception as e:
        out["events"].append({"step": "primary", "status": "failed", "reason": str(e)})
    raw = (
        json.loads((run_dir / "paddle.json").read_text())
        if (run_dir / "paddle.json").exists()
        else {"pages": []}
    )
    out["load_seconds"] = raw.get("load_seconds")
    by_path = {p["path"]: p for p in raw["pages"]}
    for page in pages:
        q = quality(page["path"])
        first = by_path.get(page["path"], {"lines": [], "model": "PP-OCRv5", "error": "主模型未完成"})
        result = {
            "id": page["id"],
            "sha256": hashlib.sha256(Path(page["path"]).read_bytes()).hexdigest(),
            "quality": q,
            "primary": first,
        }
        candidates = extract(first["lines"], page["id"], "paddle")
        risks = assess(candidates, [q])
        actionable = [r for r in risks if r["code"] != "visual_review"]
        if strategy == "cascade" and actionable and time.monotonic() - started < budget - 5:
            # Local crops when a disputed field is located; missing fields need a page fallback.
            regions = []
            for key in dict.fromkeys(r["field"] for r in actionable if r["field"]):
                if candidates.get(key):
                    box = candidates[key][0]["bbox"]
                    regions.append((key, box))
            use_full = (
                bool(first.get("error")) or not regions or any(r["code"] == "missing" for r in actionable)
            )
            targets = [(None, None)] if use_full else regions[:6]
            result["secondary"] = []
            for index, (field, box) in enumerate(targets):
                remain = budget - (time.monotonic() - started)
                if remain <= 2:
                    break
                image_path, offset = page["path"], (0, 0)
                if box:
                    with Image.open(image_path) as im:
                        crop = (
                            max(0, box[0] - 80),
                            max(0, box[1] - 45),
                            min(im.width, box[2] + 80),
                            min(im.height, box[3] + 45),
                        )
                        offset = crop[:2]
                        image_path = str(run_dir / f"crop-{page['id']}-{index}.png")
                        im.crop(crop).save(image_path)
                t = time.monotonic()
                try:
                    proc = subprocess.run(
                        [str(ROOT / "scripts/vision_ocr"), image_path],
                        capture_output=True,
                        text=True,
                        timeout=min(25, remain),
                    )
                    if proc.returncode:
                        raise RuntimeError(proc.stderr[:300])
                    lines = json.loads(proc.stdout)
                    for line in lines:
                        line["box"] = [
                            line["box"][0] + offset[0],
                            line["box"][1] + offset[1],
                            line["box"][2] + offset[0],
                            line["box"][3] + offset[1],
                        ]
                    extra = extract(lines, page["id"], "apple-vision")
                    if field in {"area", "annual"} and box:
                        for line in lines:
                            for n in re.findall(r"\d+\.\d{1,2}", line["text"]):
                                extra[field].append(
                                    {
                                        "value": n,
                                        "raw_text": line["text"],
                                        "model": "apple-vision",
                                        "source_page_id": page["id"],
                                        "bbox": line["box"],
                                        "score": line["score"],
                                        "kind": "ocr",
                                        "reason": "字段局部独立复识别",
                                        "verified": False,
                                    }
                                )
                    for key in FIELDS:
                        candidates[key].extend(extra[key])
                    result["secondary"].append(
                        {
                            "field": field,
                            "region": "full_page" if use_full else "crop",
                            "lines": lines,
                            "seconds": time.monotonic() - t,
                            "mode": "live-local",
                        }
                    )
                except Exception as e:
                    result["secondary"].append({"error": str(e), "seconds": time.monotonic() - t})
        for key in FIELDS:
            out["candidates"][key].extend(candidates[key])
        out["pages"].append(result)
        (run_dir / "checkpoint.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    out["risks"] = assess(out["candidates"], [p["quality"] for p in out["pages"]])
    out["elapsed_seconds"] = round(time.monotonic() - started, 3)
    out["state"] = "needs_human"
    out["budget_exhausted"] = time.monotonic() - started >= budget
    (run_dir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out
