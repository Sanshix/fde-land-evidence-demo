"""Batch confirmation requires agreed values, source evidence and explicit human review.

Matching model outputs are a routing signal, never proof of the source facts. Ambiguous
payments, missing evidence and extraction warnings go through the individual review flow.
"""

import math

from . import governance
from .domain import DOCUMENT_ATTRIBUTES, FIELDS, REQUIRED, normalize_mode, validate_fields
from .recognition import assess

BLOCKING_RISKS = {
    "disagreement", "cross_field_conflict", "invalid_format", "quality", "provider_unavailable",
    "missing", "no_evidence", "low_score",
}
CRITICAL = (REQUIRED | {"annual", "annual_upper", "contract_no", "doc_type"}) - {"grade"}


def agreed_fields(candidates, pages):
    fields, citations, details, problems = {}, {}, {}, []
    units = {p["id"]: p for p in pages}
    for key, label in FIELDS.items():
        rows = [c for c in candidates.get(key, []) if isinstance(c.get("value"), str) and c["value"].strip()]
        values = {c["value"].strip() for c in rows}
        if len(values) > 1:
            problems.append(f"{label}有多个候选值")
            continue
        if not values:
            continue
        value = values.pop()
        observations = [c for c in rows if c.get("kind") != "context_suggestion" and c.get("model")]
        sources = {c["model"] for c in observations}
        native = any(c.get("kind") == "native_extract" for c in observations)
        if key in CRITICAL and not native and len(sources) < 2:
            problems.append(f"{label}只有单一识别路径（上下文推断不计独立识别）")
        fields[key] = value
        citation = next((c for c in observations if c.get("kind") == "native_extract"), rows[0])
        citations[key] = citation.get("source_page_id")
        if key in DOCUMENT_ATTRIBUTES:
            continue
        for c in rows:
            page = units.get(c.get("source_page_id"))
            if not page or not str(c.get("raw_text") or "").strip():
                problems.append(f"{label}缺少当前文书的原文证据")
                continue
            if c.get("kind") == "native_extract":
                locators = str(c.get("source_locator") or "").split(" + ")
                known = {line["locator"] for line in page.get("native_lines", [])}
                if not locators or not all(loc in known for loc in locators):
                    problems.append(f"{label}原生引用不在实际段落/单元格清单内")
                elif c is citation:
                    details[key] = [{"unit_id": page["id"], "locator": loc} for loc in locators]
            else:
                box = c.get("bbox")
                if not (isinstance(box, list) and len(box) == 4
                        and all(isinstance(v, (int, float)) and math.isfinite(v) and v >= 0 for v in box)
                        and box[0] < box[2] and box[1] < box[3]):
                    problems.append(f"{label}缺少有效原文区域")
    return fields, citations, details, problems


def triage(doc):
    reasons = []
    if doc["state"] != "needs_human":
        return {"eligible": False, "reasons": ["当前状态不可批量确认"], "fields": {}, "citations": {}}
    result = doc.get("result") or {}
    if not result.get("candidates"):
        return {"eligible": False, "reasons": ["尚无识别结果"], "fields": {}, "citations": {}}
    # Recompute candidate-level risks so an old/partial result cannot bypass evidence checks.
    for risk in [*result.get("risks", []), *assess(result["candidates"])]:
        code, field = risk.get("code"), risk.get("field")
        relevant = field is None or field in CRITICAL or code in {"no_evidence", "low_score", "invalid_format"}
        if code == "no_evidence" and field in DOCUMENT_ATTRIBUTES:
            continue  # Absence/whole-document attributes need not point at a text region.
        if code in BLOCKING_RISKS and relevant:
            reasons.append(risk.get("reason", code))
    fields, citations, details, problems = agreed_fields(result["candidates"], doc.get("pages", []))
    reasons += problems
    if fields.get("altered_field") or fields.get("missing_pages") not in (None, "", "无", "否", "完整", "不缺"):
        reasons.append("识别到涂改或缺页迹象")
    if fields.get("doc_type") != "变更" and normalize_mode(fields.get("mode")) not in {"固定", "随标准调整"}:
        reasons.append("计租方式未明确勾选")
    reasons += validate_fields(fields)
    review = {"fields": fields, "citations": citations, "citation_details": details,
              "material_complete": True, "confirm_calculation": True}
    reasons += governance.validate_completeness(doc, review) + governance.validate_citations(doc, review)
    return {"eligible": not reasons, "reasons": list(dict.fromkeys(reasons)),
            "fields": fields, "citations": citations, "citation_details": details}
