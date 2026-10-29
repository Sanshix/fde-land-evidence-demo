"""Select source anchors and independent corroboration; publication still requires staff confirmation."""

from . import governance
from .domain import DOCUMENT_ATTRIBUTES, FIELDS, normalize_mode, validate_fields
from .evidence import critical_fields, normalized_candidates, observed, reported_score, valid_anchor
from .recognition import assess

BLOCKING_RISKS = {
    "disagreement", "cross_field_conflict", "invalid_format", "quality", "provider_unavailable",
    "missing", "low_score", "text_revision_review", "text_quote_unsupported", "alteration_requires_confirmation",
}
RECOMPUTED_RISKS = BLOCKING_RISKS - {"quality", "provider_unavailable", "text_revision_review", "text_quote_unsupported", "alteration_requires_confirmation"} | {"no_evidence", "score_unknown"}


def source_admission_blockers(result):
    """A unique surviving value does not erase rejected source observations."""
    readings = [result, result.get('primary') or {}, *result.get('replay_stages', []),
                *[stage for page in result.get('pages', []) for stage in page.get('stages', [])]]
    rejected = []
    for reading in readings:
        if not isinstance(reading, dict):
            continue
        diagnostics = reading.get('extraction_diagnostics') or reading.get('diagnostics') or {}
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        for observation in diagnostics.get('observations', []):
            if observation.get('rejection_reason') == 'invalid_cadastral_unit_grammar':
                rejected.append(observation)
        # Older whole-document replies did not retain per-page admission diagnostics.
        # Inspect their preserved structured values too; never substitute raw_text.
        raw_response = reading.get('raw_response')
        raw_fields = raw_response.get('fields', []) if isinstance(raw_response, dict) else []
        if isinstance(raw_fields, dict):
            raw_fields = [{**item, 'field': key} for key, item in raw_fields.items() if isinstance(item, dict)]
        for item in raw_fields if isinstance(raw_fields, list) else []:
            if (isinstance(item, dict) and item.get('field') == 'parcel_no'
                    and item.get('status') != 'cannot_determine' and isinstance(item.get('value'), str)):
                from .deterministic_parser import admit_value
                _, reason = admit_value('parcel_no', item['value'])
                if reason == 'invalid_cadastral_unit_grammar':
                    rejected.append({'field': 'parcel_no', 'raw_value': item['value'],
                                     'rejection_reason': reason, 'response_id': reading.get('response_id'),
                                     'source': 'retained_structured_response'})
    return ([{'field': 'parcel_no', 'code': 'source_admission_rejected',
              'message': '不动产单元号有不合语法的原始读数，需核对原件后采用完整值',
              'rejected_observations': rejected}] if rejected else [])


def agreed_fields(candidates, pages):
    fields, citations, details, problems, statuses = {}, {}, {}, [], {}
    units = {p["id"]: p for p in pages}
    candidates = normalized_candidates(candidates)
    critical = critical_fields(candidates, batch=True)
    for key, label in FIELDS.items():
        rows = candidates.get(key, [])
        values = {c["value"] for c in rows}
        if len(values) > 1:
            problems.append({"field": key, "code": "disagreement", "message": f"{label}有多个候选值"})
            statuses[key] = {"status": "conflict", "values": sorted(values)}
            continue
        if not values:
            if key in critical:
                problems.append({"field": key, "code": "missing", "message": f"{label}缺少可引用候选"})
            continue
        observations = [c for c in rows if observed(c, units)]
        sources = {c["model"] for c in observations}
        anchors = [c for c in observations if valid_anchor(c, units)]
        native = next((c for c in anchors if c.get("kind") == "native_extract"), None)
        anchor = native or next(iter(anchors), None)
        fields[key] = values.pop()
        statuses[key] = {
            "status": "anchored" if anchor else "missing_anchor",
            "anchor_unit": anchor.get("source_page_id") if anchor else None,
            "identifier_formats": [c["identifier_format"] for c in rows if "identifier_format" in c],
            "independent_paths": len(sources),
            "unknown_score_paths": sum(reported_score(c) is None for c in observations),
            "corroborating_without_region": sum(not valid_anchor(c, units) for c in observations),
        }
        if key in critical and not native and len(sources) < 2:
            problems.append({"field": key, "code": "single_path",
                             "message": f"{label}只有单一识别路径（上下文推断不计独立识别）"})
        if any(reported_score(c) is not None and reported_score(c) < 0.9 for c in rows):
            problems.append({"field": key, "code": "low_score", "message": f"{label}存在真实低分候选，需核原件"})
        if key in DOCUMENT_ATTRIBUTES:
            if not observations:
                problems.append({"field": key, "code": "no_evidence", "message": f"{label}缺少当前文书观察记录"})
            continue
        if not anchor:
            problems.append({"field": key, "code": "no_evidence", "message": f"{label}缺少有效主证据区域或原生位置"})
            continue
        citations[key] = anchor["source_page_id"]
        if anchor.get("kind") == "native_extract":
            details[key] = [{"unit_id": anchor["source_page_id"], "locator": loc}
                            for loc in anchor["source_locator"].split(" + ")]
        else:
            details[key] = [{"unit_id": anchor["source_page_id"], "bbox": anchor["bbox"]}]
    return fields, citations, details, problems, statuses


def evidence_gate(doc):
    def unavailable(message):
        return {"eligible": False, "reasons": [message], "fields": {}, "citations": {},
                "blockers": [{"field": None, "code": "unavailable", "message": message}], "evidence_status": {}}

    if doc["state"] != "needs_human":
        return unavailable("当前状态不可批量确认")
    result = doc.get("result") or {}
    if not result.get("candidates"):
        return unavailable("尚无识别结果")
    candidates = normalized_candidates(result["candidates"])
    critical = critical_fields(candidates, batch=True)
    blockers = source_admission_blockers(result)
    # Recompute candidate-level flags; keep source-quality flags. Never rewrite historical results.
    risks = [r for r in result.get("risks", []) if r.get("code") not in RECOMPUTED_RISKS]
    risks += assess(candidates)
    for risk in risks:
        code, field = risk.get("code"), risk.get("field")
        relevant = field is None or field in critical or code in {"low_score", "invalid_format", "disagreement"}
        if code in BLOCKING_RISKS and relevant:
            blockers.append({"field": field, "code": code, "message": risk.get("reason", code)})
    if result.get("completion_status") in {"partial", "unknown", "failed"}:
        blockers.append({"field": None, "code": "recognition_incomplete", "message": "识别仍有未完成或结果未知路径，需恢复或逐项复核"})
    fields, citations, details, problems, statuses = agreed_fields(candidates, doc.get("pages", []))
    blockers += problems
    readings = [result.get("primary", {}), *[s for p in result.get("pages", []) for s in p.get("stages", [])]]
    if any(reading.get("warnings") for reading in readings):
        blockers.append({"field": None, "code": "source_warning", "message": "识别路径报告原件疑点，需逐项核验后处理"})
    if fields.get("altered_field") or fields.get("missing_pages") not in (None, "", "无", "否", "完整", "不缺"):
        blockers.append({"field": None, "code": "visual_ambiguity", "message": "识别到涂改或缺页迹象"})
    if any(c.get("text_role") in {"crossed_out", "alternative"} for rows in candidates.values() for c in rows):
        blockers.append({"field": None, "code": "visual_ambiguity", "message": "存在涂改前或备选原文，需逐项核验"})
    if fields.get("doc_type") != "变更" and normalize_mode(fields.get("mode")) not in {"固定", "随标准调整"}:
        blockers.append({"field": "mode", "code": "checkbox_unresolved", "message": "计租方式缺少明确条款或勾选证据"})
    review = {"fields": fields, "citations": citations, "citation_details": details,
              "material_complete": True, "confirm_calculation": True}
    for message in (validate_fields(fields) + governance.validate_completeness(doc, review)
                    + governance.validate_citations(doc, review)):
        blockers.append({"field": None, "code": "validation", "message": message})
    reasons = list(dict.fromkeys(b["message"] for b in blockers))
    return {"eligible": not reasons, "reasons": reasons, "blockers": blockers, "evidence_status": statuses,
            "fields": fields, "citations": citations, "citation_details": details}


def triage(doc):
    from . import confidence
    gate = evidence_gate(doc)
    profile = confidence.machine(doc, gate['blockers'])
    if gate['eligible'] and not profile['batch_eligible']:
        gate['eligible'] = False
        gate['reasons'].append('可信度未达到批量门槛：综合≥95%，全部关键字段≥90%')
        gate['blockers'].append({'field': None, 'code': 'confidence_gate', 'message': gate['reasons'][-1]})
    gate['confidence'] = confidence.restrict(profile, gate['blockers'])
    return gate
