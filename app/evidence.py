"""Comparison and source checks shared by routing and review, never a truth oracle."""

import math
from decimal import Decimal, InvalidOperation

from .domain import CHANGE_REQUIRED, REQUIRED
from .governance import normalize


def comparison_value(key, value):
    """Normalize syntax only. No registry substitution, fuzzy names, or parcel repair."""
    values = normalize(key, value)
    value = values[0] if len(values) == 1 else str(value).strip()
    if key in {"area", "annual", "rate_standard"}:
        try:
            number = Decimal(value)
            if number.is_finite():
                return format(number.normalize(), "f")
        except InvalidOperation:
            pass
    if key == "identity":
        return value.upper()
    return value


def normalized_candidates(candidates):
    from .identifier_templates import comparison_key, describe

    result = {}
    for key, rows in candidates.items():
        prepared, representatives = [], {}
        for c in rows:
            if not isinstance(c, dict) or not isinstance(c.get("value"), str) or not c["value"].strip():
                continue
            value = comparison_value(key, c["value"])
            item = {**c, "raw_value": c.get("raw_value", c["value"]), "value": value}
            if key in {"contract_no", "original_contract_no"}:
                original = c.get("identifier_format", {}).get("ocr_value", c["value"])
                info = describe(original)
                group = comparison_key(value)
                representative = representatives.setdefault(group, info["normalized_value"] or value)
                item.update(value=representative, identifier_format=info)
            prepared.append(item)
        result[key] = prepared
    return result


def reported_score(candidate):
    # Includes legacy OCR results stored with score=0, score_meaning=not_provided.
    if candidate.get("score_meaning") == "not_provided":
        return None
    value = candidate.get("score")
    return value if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1 else None


def observed(candidate, pages=None):
    if candidate.get("kind") == "context_suggestion" or not candidate.get("model"):
        return False
    if not str(candidate.get("raw_text") or "").strip() or not candidate.get("source_page_id"):
        return False
    return pages is None or candidate["source_page_id"] in pages


def valid_anchor(candidate, pages=None):
    if not observed(candidate, pages):
        return False
    if candidate.get("kind") == "native_extract":
        locators = str(candidate.get("source_locator") or "").split(" + ")
        if not all(locators):
            return False
        if pages is not None:
            known = {line["locator"] for line in pages[candidate["source_page_id"]].get("native_lines", [])}
            return all(loc in known for loc in locators)
        return True
    box = candidate.get("bbox")
    if not (isinstance(box, list) and len(box) == 4
            and all(type(v) in {int, float} and math.isfinite(v) and v >= 0 for v in box)
            and box[0] < box[2] and box[1] < box[3]):
        return False
    if pages is not None:
        page = pages[candidate["source_page_id"]]
        if (page.get("width") and box[2] > page["width"]) or (page.get("height") and box[3] > page["height"]):
            return False
    return True


def critical_fields(candidates, *, batch=False):
    types = {comparison_value("doc_type", c.get("value", "")) for c in candidates.get("doc_type", [])}
    if types == {"变更"}:
        required = set(CHANGE_REQUIRED)
    else:
        required = set(REQUIRED)
        modes = {comparison_value("mode", c.get("value", "")) for c in candidates.get("mode", [])}
        if modes != {"随标准调整"}:
            required.add("annual")
    if batch:
        required |= {"doc_type"}
        # Optional scored fields still need independent corroboration when present.
        required |= {k for k in {"annual_upper", "signed", "identity"} if candidates.get(k)}
    return required
