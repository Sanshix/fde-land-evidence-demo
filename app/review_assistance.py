"""Read-only review aids. Candidate routing never establishes a business fact."""

import math
from collections import defaultdict

from .domain import ALTERABLE, FIELDS, cadastre


def _fields(doc, draft=None):
    fields = dict((draft or doc.get("draft") or doc.get("review") or {}).get("fields", {}))
    for key, rows in (doc.get("result") or {}).get("candidates", {}).items():
        values = {c["value"].strip() for c in rows if isinstance(c.get("value"), str) and c["value"].strip()}
        if not fields.get(key) and len(values) == 1:
            fields[key] = values.pop()
    return fields


def _box(value):
    if (isinstance(value, list) and len(value) == 4
            and all(type(v) in {int, float} and math.isfinite(v) and v >= 0 for v in value)
            and value[0] < value[2] and value[1] < value[3]):
        return value
    return None


def assistance(doc):
    candidates = (doc.get("result") or {}).get("candidates", {})
    fields = _fields(doc)
    altered_label = fields.get("altered_field", "")
    target = ALTERABLE.get(altered_label)
    altered_keys = [key for key, rows in candidates.items()
                    if any(c.get("text_role") in {"crossed_out", "alternative"} for c in rows)]
    if not target and len(altered_keys) == 1:
        target = altered_keys[0]
    units = {p["id"]: p for p in doc.get("pages", [])}
    observations = []
    for key in dict.fromkeys([*altered_keys, target, "altered_before"]):
        if not key:
            continue
        for c in candidates.get(key, []):
            if c.get("source_page_id") not in units or not str(c.get("value") or "").strip():
                continue
            observations.append({"field": key, "label": FIELDS.get(key, key), "value": c["value"],
                                 "role": "crossed_out" if key == "altered_before" else c.get("text_role", "unspecified"),
                                 "page_id": c["source_page_id"], "bbox": _box(c.get("bbox")),
                                 "raw_text": c.get("raw_text", "")})
    seal_regions = [{"field": "altered_sealed", "label": "签章候选区域", "value": c.get("value", ""),
                     "role": "seal", "page_id": c["source_page_id"], "bbox": _box(c.get("bbox")),
                     "raw_text": c.get("raw_text", "")}
                    for c in candidates.get("altered_sealed", [])
                    if c.get("source_page_id") in units and _box(c.get("bbox"))]
    return {"altered": bool(altered_label or altered_keys), "field": target,
            "candidates": observations, "seal_regions": seal_regions,
            "seal_region_note": "签章区域为模型候选，请同时核对涂改位置与原件。" if seal_regions
            else "未取得签章区域定位，请在完整原件中核对手印或盖章。",
            "amount_impact_note": "金额影响以采纳原文后的租金预览为准；候选差异不代表修改有效。"}


def prioritize(docs, drafts=None):
    """Order known material chains first; no fuzzy names or knowledge of missing originals."""
    drafts = drafts or {}
    groups = defaultdict(list)
    metadata = {}
    for doc in docs:
        fields = _fields(doc, drafts.get(doc["id"]))
        key = cadastre(fields.get("parcel_no"))
        group = "-".join(map(str, key)) if key else None
        if group:
            groups[group].append(doc)
        risks = (doc.get("result") or {}).get("risks", [])
        complexity = len({(r.get("code"), r.get("field")) for r in risks
                          if r.get("code") not in {"visual_review", "score_unknown"}})
        metadata[doc["id"]] = (group, complexity)
    priorities = {}
    for doc in docs:
        if doc["state"] in {"published", "withdrawn"}:
            continue
        group, complexity = metadata[doc["id"]]
        related = groups.get(group, [])
        pending = sum(d["state"] not in {"published", "withdrawn"} for d in related)
        published = sum(d["state"] == "published" for d in related)
        if published and pending == 1:
            tier, reason = 0, "可补齐当前已知材料链：本组已有登记，仅余此份待处理"
        elif published:
            tier, reason = 1, f"本组已有登记，另有 {pending} 份已知材料待处理"
        else:
            tier, reason = 2, "优先处理问题较少、判断明确的材料"
        if doc["state"] in {"queued", "recognizing"}:
            tier, reason = 3, "识别仍在处理中，可先核验其他材料"
        priorities[doc["id"]] = {"rank": tier * 10000 + min(complexity, 9999), "reason": reason,
                                    "group_key": group, "pending_in_group": pending,
                                    "published_in_group": published,
                                    "grouping_basis": "已采纳或唯一候选宗地号，分组仍需人工核对" if group else "宗地尚未明确"}
    return priorities
