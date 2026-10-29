"""Source-aware extraction and explicit data quality states; no ground-truth access."""

import hashlib
import os
import re
import unicodedata
from datetime import date

from .domain import DOCUMENT_ATTRIBUTES, FIELDS, clean
from .extraction import merge, prose_candidates

ALIASES = {
    "doc_type": ["合同类型"],
    "contract_no": ["合同编号", "合同号"],
    "tenant": ["承租人", "承租方"],
    "identity": ["证件号码", "证件号"],
    "phone": ["联系电话", "联系电话号码"],
    "parcel_no": ["不动产单元号", "宗地号", "地号"],
    "address": ["土地坐落", "坐落"],
    "area": ["土地面积", "面积"],
    "use": ["土地用途", "用途"],
    "grade": ["土地级别"],
    "start": ["租赁起始日", "租赁起始日期"],
    "end": ["租赁终止日", "租赁终止日期"],
    "mode": ["计租方式"],
    "annual": ["年租金（小写）", "年租金(小写)", "年租金小写", "年租金"],
    "annual_upper": ["年租金（大写）", "年租金(大写)", "年租金大写"],
    "signed": ["签订日期"],
}


def issue_id(problem):
    return hashlib.sha256(
        (
            problem.get("code", "")
            + "|"
            + problem.get("location", "")
            + "|"
            + problem.get("message", problem.get("reason", ""))
        ).encode()
    ).hexdigest()[:16]


def normalize(key, raw):
    text = unicodedata.normalize("NFKC", str(raw)).strip().strip("：:；;。")
    if os.environ.get("FDE_FIELD_CLEANUP", "1") == "1":
        if key == "tenant":
            text = re.sub(r"^\s*[（(]?乙方[)）]?\s*[:：]?\s*", "", text)
            text = re.split(r"证件号码|联系电话|住所[:：]", text)[0].strip()
        if key == "identity":
            text = re.split(r"联系电话|联系号码|电话[:：]", text)[0].strip()
        if key in {"area", "annual"}:
            # Unit exponents are not additional numeric observations.
            text = re.sub(r"(?:m|M)\s*(?:\^\s*)?2|㎡", "平方米", text)
        if key in {"start", "end", "signed"}:
            from .extraction import CN_DATE, cn_date
            text = re.sub(r"签订日期[:：]?", "", text).strip()
            observed = []
            for match in re.finditer(CN_DATE, text):
                value = cn_date(match)
                if value:
                    observed.append(value)
            numeric_date = r"(\d{4})[./-](\d{1,2})[./-](\d{1,2})"
            for match in re.finditer(numeric_date, text):
                try:
                    observed.append(date(*map(int, match.groups())).isoformat())
                except ValueError:
                    return [text]
            remainder = re.sub(CN_DATE, "", text)
            remainder = re.sub(numeric_date, "", remainder).strip(" ,，;；:：|\t\n")
            if observed and len(set(observed)) == 1 and not remainder:
                return [observed[0]]
        if key == "doc_type":
            from .extraction import document_type_observation
            value, _ = document_type_observation([text])
            if value:
                return [value]
    if key in {"area", "annual"}:
        # Preserve signs: turning a source '-120' into '+120' invents a fact.
        values = re.findall(r"(?<![\d.])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", text)
        return [v.replace(",", "").replace("−", "-") for v in values]
    if key in {"start", "end", "signed"}:
        m = re.fullmatch(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?", text)
        if m:
            try:
                return [date(*map(int, m.groups())).isoformat()]
            except ValueError:
                return [text]
    if key == "use":
        return [v for v in ["住宅", "商服", "工业", "仓储"] if v in text] or [text]
    if key == "grade":
        return re.findall(r"[一二三四]级", text) or [text]
    if key == "mode":
        text = {"随基准地价调整": "随标准调整", "随基准地价标准调整": "随标准调整"}.get(text, text)
        # A menu listing both choices is NOT a selection.
        if "固定" in text and "随标准调整" in text:
            return []
        if text in {"固定", "随标准调整"}:
            return [text]
        from .field_semantics import explicit_modes
        return explicit_modes(text)
    return [text] if text else []


def native_candidates(units):
    out = {k: [] for k in FIELDS}
    for u in units:
        lines = u.get("native_lines", [])
        for index, line in enumerate(lines):
            raw = line.get("text", "")
            if line.get("formula") is not None or line.get("hidden") or line.get("tracked_change"):
                continue  # Visible as evidence but never silently parsed into accepted facts.
            all_labels = sorted(
                ((alias, key) for key, aliases in ALIASES.items() for alias in aliases),
                key=lambda x: -len(x[0]),
            )
            found = next(
                ((alias, key) for alias, key in all_labels if clean(raw).startswith(clean(alias))), None
            )
            if not found:
                continue
            alias, key = found
            prefix = re.match(r"^\s*" + r"\s*".join(re.escape(c) for c in alias), raw)
            if not prefix:
                continue
            value = raw[prefix.end() :].strip(":：\t ")
            normalized_input = value
            loc = line["locator"]
            evidence = raw
            if not value and index + 1 < len(lines):
                following = lines[index + 1]
                text = following.get("text", "")
                if (
                    following.get("formula") is not None
                    or following.get("hidden")
                    or any(clean(text).startswith(clean(a)) for a, _ in all_labels)
                ):
                    continue
                value = text
                normalized_input = following.get("display_value", text)
                if (
                    key in {"start", "end", "signed"}
                    and following.get("display_status") == "date_interpretation_check_epoch"
                ):
                    normalized_input = normalized_input.split("T")[0]
                loc += " + " + following["locator"]
                evidence += " | " + text
            normalized_values = normalize(key, normalized_input)
            if os.environ.get("FDE_FIELD_CLEANUP", "1") == "1" and key in {"area", "annual"} and len(normalized_values) > 1:
                # More than one number is an ambiguity, not several isolated facts.
                normalized_values = []
            for normalized in normalized_values:
                if not normalized or re.search(r"【.*?】|待填写|待确认", normalized):
                    continue
                out[key].append(
                    {
                        "value": normalized,
                        "raw_text": evidence,
                        "raw_value": value,
                        "normalization": "NFKC/explicit date or numeric token only; raw retained",
                        "source_locator": loc,
                        "source_page_id": u["id"],
                        "bbox": None,
                        "score": 1.0,
                        "score_meaning": "mechanical_extraction_not_model_confidence",
                        "kind": "native_extract",
                        "model": "native-structure-v1",
                        "provider": "local_parser_no_model",
                        "reason": "按原生标签/相邻结构提取，内容与业务正确性仍需人核验",
                        "verified": False,
                        "can_publish": False,
                    }
                )
    return out


def issues_for(doc):
    issues = []
    for original in (doc.get("intake") or {}).get("issues", []):
        issues.append({**original, "category": "原件问题", "id": issue_id(original)})
    for risk in (doc.get("result") or {}).get("risks", []):
        category = "业务冲突" if risk.get("code") in {"disagreement", "cross_field_conflict"} else "识别问题"
        p = {
            **risk,
            "location": risk.get("field") or "document",
            "message": risk.get("reason", ""),
            "category": category,
        }
        p["id"] = issue_id(p)
        issues.append(p)
    if doc["state"] in {"queued", "recognizing", "held"}:
        p = {
            "code": doc["state"],
            "location": "document",
            "message": {
                "queued": "识别任务未完成",
                "recognizing": "识别执行中",
                "held": "已挂起，等待补件/核验",
            }[doc["state"]],
            "category": "未完成",
        }
        p["id"] = issue_id(p)
        issues.append(p)
    if not issues:
        p = {
            "code": "human_pending",
            "location": "document",
            "message": "技术提取完成仍须人工确认资料完整与业务事实",
            "category": "待确认",
        }
        p["id"] = issue_id(p)
        issues.append(p)
    return issues


def validate_alteration_review(doc, body):
    review = body.get("alteration_review")
    if review is None:
        return []  # Existing API callers still record their decision via fields + staff confirmation.
    if not isinstance(review, dict):
        return ["签章核验记录格式无效"]
    if not (body.get("fields") or {}).get("altered_field"):
        return []
    if review.get("decision") not in {"present", "absent"}:
        return ["涂改处签章尚无法判断，请挂起并补充核验"]
    if review.get("unit_id") not in {p["id"] for p in doc["pages"]}:
        return ["签章核验须选择当前文书的原件页"]
    expected = "是" if review["decision"] == "present" else "否"
    if (body.get("fields") or {}).get("altered_sealed") != expected:
        return ["签章核验结论与采纳字段不一致"]
    return []


def validate_completeness(doc, body):
    intake = doc.get("intake") or {}
    if intake.get("technical_status") == "failed":
        return ["资料解析失败/不支持，需补件或转换格式；不能标记完整"]
    errors = validate_alteration_review(doc, body)
    if not body.get("material_complete"):
        errors.append("请独立核对页/表/单元格与嵌入图清单完整性")
    if not body.get("confirm_calculation"):
        errors.append("请完成第二道核算复核，检查逐年依据与缴费归集")
    pages = {p["id"] for p in doc["pages"]}
    resolutions = body.get("quality_resolutions", {})
    for p in intake.get("issues", []):
        if p.get("severity") != "blocking":
            continue
        resolution = resolutions.get(issue_id(p), {})
        if (
            resolution.get("decision") not in {"verified_with_evidence", "excluded_with_reason"}
            or not resolution.get("reason", "").strip()
            or resolution.get("source_unit") not in pages
        ):
            errors.append("需记录证据和处理理由：" + p["message"])
    return errors


def validate_citations(doc, body):
    pages = {p["id"]: p for p in doc["pages"]}
    details = body.get("citation_details", {})
    errors = []
    for key, value in body.get("fields", {}).items():
        if not value or key in DOCUMENT_ATTRIBUTES:
            continue
        primary = body.get("citations", {}).get(key)
        if primary not in pages:
            errors.append(f"{FIELDS.get(key, key)}缺少有效证据单元")
            continue
        refs = details.get(key, [])
        for ref in refs:
            page = pages.get(ref.get("unit_id"))
            if not page:
                errors.append("引用了当前文书之外的证据")
                continue
            loc = ref.get("locator", "")
            if page.get("native_lines") and loc not in {line["locator"] for line in page["native_lines"]}:
                errors.append("原生证据位置不在实际段落/单元格清单内")
        if pages[primary].get("kind") == "native" and not any(
            r.get("unit_id") == primary and r.get("locator") for r in refs
        ):
            errors.append(f"{FIELDS.get(key, key)}需精确到原生段落/表格单元格")
    return errors


def parse_ocr_text(text, page, profile):
    """Separate observed OCR protocol markers from visible numeric facts; retain raw output."""
    from .deterministic_parser import parse_text

    return parse_text(text, page, profile)


def legacy_parse_ocr_text(text, page, profile):
    """Pre-admission parser retained for explicit offline diagnosis, never selected implicitly."""
    lines, seen, markers, repeated = [], set(), 0, 0
    for index, raw in enumerate(text.splitlines(), 1):
        clean_line, count = re.subn(r"<\|LOC_\d+\|>", "", raw)
        markers += count
        if clean_line in seen:
            repeated += bool(clean_line.strip())
            continue
        seen.add(clean_line)
        lines.append({"text": clean_line, "raw_text": raw, "locator": f"response/line{index}"})
    candidates = native_candidates([{"id": page["id"], "native_lines": lines}])
    for rows in candidates.values():
        for candidate in rows:
            candidate.update(
                kind="ocr",
                model=profile["model_id"],
                provider=profile["provider"],
                score=None,
                score_meaning="not_provided",
                source_locator=None,
                response_locator=candidate["source_locator"],
                reason="云OCR转录按标签抽取；位置协议标记不当业务数字，重复行去重；未验证原图坐标，需核原件",
                evidence_status="missing_region",
            )
    candidates = merge(candidates, prose_candidates(lines, page["id"], profile["model_id"], profile["provider"]))
    return candidates, {
        "parser_version": "ocr-protocol-clean-v2",
        "location_markers_removed_from_extraction": markers,
        "duplicate_lines_excluded_from_extraction": repeated,
        "raw_output_retained": True,
    }
