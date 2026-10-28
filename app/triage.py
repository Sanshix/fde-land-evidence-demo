"""分流：哪些材料可由经办人员批量确认（“自动通过”），哪些必须对照原件逐项核对（“人工复核”）。

口径（使用说明·自测评分）：自动通过且正确得 1，送人工复核得 0.5，自动通过但有错扣 1。所以只有
“两条独立识别路径给出同一值”或“原生电子文档结构化提取”的字段才算有把握；任何多值、缺值、
格式不合、图像质量告警、原件阻断项、涂改/缺页/未勾选迹象都送人工复核。
"""

from .domain import FIELDS, REQUIRED, normalize_mode, validate_fields

BLOCKING_RISKS = {"disagreement", "cross_field_conflict", "invalid_format", "quality", "provider_unavailable", "missing"}
CRITICAL = (REQUIRED | {"annual", "annual_upper", "contract_no", "doc_type"}) - {"grade"}


def agreed_fields(candidates):
    fields, citations, problems = {}, {}, []
    for key, label in FIELDS.items():
        rows = candidates.get(key, [])
        values = {str(c["value"]).strip() for c in rows if str(c.get("value", "")).strip()}
        if len(values) > 1:
            problems.append(f"{label}有多个候选值")
            continue
        if not values:
            continue
        value = values.pop()
        sources = {c.get("model") for c in rows}
        native = any(c.get("kind") == "native_extract" for c in rows)
        if key in CRITICAL and not native and len(sources) < 2:
            problems.append(f"{label}只有单一识别路径")
        fields[key] = value
        citations[key] = rows[0].get("source_page_id")
    return fields, citations, problems


def triage(doc):
    reasons = []
    if doc["state"] != "needs_human":
        return {"eligible": False, "reasons": ["当前状态不可批量确认"], "fields": {}, "citations": {}}
    result = doc.get("result") or {}
    if not result.get("candidates"):
        return {"eligible": False, "reasons": ["尚无识别结果"], "fields": {}, "citations": {}}
    intake = doc.get("intake") or {}
    if any(i.get("severity") == "blocking" for i in intake.get("issues", [])):
        reasons.append("原件有阻断项")
    for risk in result.get("risks", []):
        if risk.get("code") in BLOCKING_RISKS and (risk.get("field") in CRITICAL or risk.get("field") is None):
            reasons.append(risk.get("reason", risk["code"]))
    fields, citations, problems = agreed_fields(result["candidates"])
    reasons += problems
    if fields.get("altered_field") or fields.get("missing_pages"):
        reasons.append("识别到涂改或缺页迹象")
    if fields.get("doc_type") != "变更" and normalize_mode(fields.get("mode")) not in {"固定", "随标准调整"}:
        reasons.append("计租方式未明确勾选")
    reasons += validate_fields(fields)
    return {"eligible": not reasons, "reasons": list(dict.fromkeys(reasons)), "fields": fields, "citations": citations}
