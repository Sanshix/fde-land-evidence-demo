"""Extract observed contract fields from Baidu layout blocks without inventing cell geometry."""

import math
import re
import unicodedata
from datetime import date
from html.parser import HTMLParser

from .domain import FIELDS
from .extraction import document_type_observation, prose_candidates
from .governance import normalize, parse_ocr_text

LABELS = {
    "合同类型": "doc_type", "合同编号": "contract_no", "合同号": "contract_no", "原合同编号": "original_contract_no",
    "出租人": "lessor", "出租人(甲方)": "lessor", "承租人": "tenant", "承租人(乙方)": "tenant", "新承租人": "tenant", "变更后承租人": "tenant",
    "证件号码": "identity", "证件号": "identity", "联系电话": "phone", "住所": "residence",
    "地号": "parcel_no", "宗地号": "parcel_no", "不动产单元号": "parcel_no", "坐落": "address", "土地坐落": "address",
    "土地面积": "area", "面积": "area", "土地用途": "use", "用途": "use", "土地级别": "grade",
    "租赁期限": "term", "租赁起始日": "start", "租赁终止日": "end", "签订日期": "signed",
    "计租方式": "mode", "年租金标准": "rate_standard", "年租金": "annual", "年租金(小写)": "annual",
    "年租金小写": "annual", "年租金(大写)": "annual_upper", "年租金大写": "annual_upper",
}
DATE_PATTERN = r"(\d{4})\s*[年./-]\s*(\d{1,2})\s*[月./-]\s*(\d{1,2})日?"


class _TableText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.outside = [], []
        self.row, self.cell = None, None
        self.ignore = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.ignore += 1
        elif tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            self.cell = []
        elif tag == "br":
            (self.cell if self.cell is not None else self.outside).append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.ignore = max(0, self.ignore - 1)
        elif tag in {"td", "th"} and self.cell is not None:
            if self.row is not None:
                self.row.append("".join(self.cell).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag in {"p", "div", "table", "h1", "h2", "h3"}:
            self.outside.append("\n")

    def handle_data(self, text):
        if not self.ignore:
            (self.cell if self.cell is not None else self.outside).append(text)


def _label(value):
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).strip(":：")
    if value.startswith(("土地面积(", "面积(")) and value.endswith(")"):
        value = value.split("(", 1)[0]
    return LABELS.get(value)


def _thumbnail_size(width, height):
    # Same aspect-preserving integer rounding as Pillow Image.thumbnail((2200,2200)).
    if width <= 2200 and height <= 2200:
        return width, height
    aspect = width / height
    x = y = 2200
    if aspect <= 1:
        number = y * aspect
        x = max(min(math.floor(number), math.ceil(number), key=lambda n: abs(aspect - n / y)), 1)
    else:
        number = x / aspect
        y = max(min(math.floor(number), math.ceil(number), key=lambda n: 0 if n == 0 else abs(aspect - x / n)), 1)
    return x, y


def _coordinate_frame(page, original_box):
    if not (isinstance(original_box, (list, tuple)) and len(original_box) == 4
            and all(type(v) in {int, float} and math.isfinite(v) and v >= 0 and int(v) == v for v in original_box)
            and original_box[0] < original_box[2] and original_box[1] < original_box[3]):
        return None
    if page.get("page_angle", 0) != 0 or page.get("page_angle_warning"):
        return None
    if page.get("display_size_ratio", 1) != 1:
        return None
    meta = page.get("meta") or {}
    if not isinstance(meta, dict):
        return None
    for owner in (page, meta):
        if any(owner.get(key) not in (None, False, 0, "", "none", "identity")
               for key in ("rotation", "rotate_angle", "orientation", "deskew_angle", "crop", "transform", "transformation")):
            return None
    width, height = meta.get("page_width", page.get("width")), meta.get("page_height", page.get("height"))
    expected = _thumbnail_size(int(original_box[2] - original_box[0]), int(original_box[3] - original_box[1]))
    # Without provider frame dimensions the block geometry remains unverified.
    if (width, height) != expected:
        return None
    return width, height


def _block_box(block, frame, original_box):
    if frame is None:
        return None
    width, height = frame
    position = block.get("position")
    box = None
    if isinstance(position, list) and len(position) == 4 and all(type(v) in {int, float} and math.isfinite(v) for v in position):
        x, y, w, h = position
        if w > 0 and h > 0 and 0 <= x < x + w <= width and 0 <= y < y + h <= height:
            box = [x, y, x + w, y + h]
    polygon = block.get("polygon")
    if polygon is not None:
        if not (isinstance(polygon, list) and len(polygon) >= 4
                and all(isinstance(p, list) and len(p) == 2 and all(type(v) in {int, float} and math.isfinite(v) for v in p) for p in polygon)):
            return None
        xs, ys = [p[0] for p in polygon], [p[1] for p in polygon]
        poly_box = [min(xs), min(ys), max(xs), max(ys)]
        if not (0 <= poly_box[0] < poly_box[2] <= width and 0 <= poly_box[1] < poly_box[3] <= height):
            return None
        if box and any(abs(a - b) > 2 for a, b in zip(box, poly_box, strict=True)):
            return None
        box = box or poly_box
    if box is None:
        return None
    return [original_box[0] + box[0] / width * (original_box[2] - original_box[0]),
            original_box[1] + box[1] / height * (original_box[3] - original_box[1]),
            original_box[0] + box[2] / width * (original_box[2] - original_box[0]),
            original_box[1] + box[3] / height * (original_box[3] - original_box[1])]


def baidu_candidates(document, page, profile, original_box):
    """Return candidates and protocol diagnostics for one explicitly submitted image."""
    from .deterministic_parser import VERSION, admit_value, parse_lines, response_identity, trace

    out = {key: [] for key in FIELDS}
    diagnostics = {"parser_version": VERSION, "coordinate_frame_verified": False,
                   "layout_blocks": 0, "table_rows": 0, "unmapped_blocks": 0,
                   "layout_score_is_not_text_confidence": True, "observations": [], "parser_disagreements": []}
    response_id = response_identity(document, profile)
    pages = document.get("pages") if isinstance(document, dict) else None
    if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0], dict):
        return out, {**diagnostics, "rejected": "single_input_page_response_count_mismatch"}
    response_page = pages[0]
    frame = _coordinate_frame(response_page, original_box)
    diagnostics["coordinate_frame_verified"] = frame is not None
    layouts = response_page.get("layouts") or []
    layouts = layouts if isinstance(layouts, list) else []
    table_rows = response_page.get("tables") or []
    table_rows = table_rows if isinstance(table_rows, list) else []
    tables = {t.get("layout_id"): t for t in table_rows if isinstance(t, dict)}
    blocks = [(b, f"response/pages/0/layouts/{i}") for i, b in enumerate(layouts) if isinstance(b, dict)]
    # Some responses expose tables outside layouts. They have no trustworthy region
    # unless their own position and frame satisfy exactly the same mapping checks.
    known = {b.get("layout_id") for b, _ in blocks if b.get("layout_id")}
    blocks += [(t, f"response/pages/0/tables/{i}") for i, t in enumerate(table_rows)
               if isinstance(t, dict) and t.get("layout_id") not in known]
    if not blocks:
        text = response_page.get("text")
        if not isinstance(text, str):
            return out, diagnostics
        if "<table" not in text.lower():
            candidates, text_diagnostics = parse_ocr_text(text, page, profile)
            return candidates, {**diagnostics, "text_only": True, "text_diagnostics": text_diagnostics}
        blocks = [({"text": text}, "response/pages/0/text")]
    observed_text = []

    def add(key, value, raw, locator, bbox, *, text_role="unspecified"):
        if key not in out or not isinstance(value, str) or not value.strip() or len(value) > 160:
            return
        normalized, rejection = admit_value(key, value)
        provenance = trace(key, raw[1], normalized, rejection, locator, response_id, scope='block_or_cell')
        diagnostics['observations'].append(provenance)
        if len(normalized) != 1:
            return
        value = normalized[0]
        if any(c["value"] == value and c.get("response_locator") == locator for c in out[key]):
            return
        out[key].append({"value": value, "raw_value": raw[1], "raw_text": raw[0][:1000],
            "model": profile["model_id"], "provider": profile["provider"], "source_page_id": page["id"],
            "source_locator": None, "response_locator": locator, "bbox": bbox, "score": None,
            "parse_provenance": provenance,
            "score_meaning": "not_provided", "kind": "ocr", "verified": False, "can_publish": False,
            "text_role": text_role, "evidence_status": "provider_block_needs_human" if bbox else "missing_region",
            "reason": "百度版面块原文抽取；区域为整块非精确单元格，布局分数不代表文字正确率，须核原件"})

    def labeled(key, value, raw, locator, bbox):
        if key == "term":
            matches = list(re.finditer(DATE_PATTERN, value))
            if len(matches) == 2 and re.search(r"至|到|—|~|～", value[matches[0].end():matches[1].start()]):
                for field, match in zip(("start", "end"), matches, strict=True):
                    try:
                        parsed = date(*map(int, match.groups())).isoformat()
                    except ValueError:
                        continue
                    add(field, parsed, (raw, value), locator, bbox)
            return
        if key == "mode":
            modes = prose_candidates([{"text": raw, "locator": locator}], page["id"], profile["model_id"], profile["provider"])["mode"]
            for item in modes:
                add("mode", item["value"], (raw, value), locator, bbox)
            return
        values, rejection = admit_value(key, value)
        if key == "rate_standard":
            numeric = re.sub(r"元\s*/.*|/\s*(?:\$\s*)?(?:m|平方米).*", "", unicodedata.normalize("NFKC", value))
            numeric = re.sub(r"(?<=\d)\.\s+(?=\d)", ".", numeric)
            values, rejection = admit_value(key, numeric)
        if not values:
            diagnostics['observations'].append(trace(key, value, [], rejection, locator, response_id,
                                                      scope='block_or_cell'))
        for normalized in values:
            add(key, normalized, (raw, value), locator, bbox)

    for block, locator in blocks:
        raw = block.get("table_html") or block.get("text") or tables.get(block.get("layout_id"), {}).get("table_html")
        if not isinstance(raw, str) or len(raw) > 150000:
            continue
        bbox = _block_box(block, frame, original_box)
        diagnostics["layout_blocks"] += 1
        diagnostics["unmapped_blocks"] += bbox is None
        parser = _TableText()
        parser.feed(raw)
        diagnostics["table_rows"] += len(parser.rows)
        for row_index, cells in enumerate(parser.rows):
            for cell_index in range(len(cells) - 1):
                key = _label(cells[cell_index])
                if key and cells[cell_index + 1] and not _label(cells[cell_index + 1]):
                    value = cells[cell_index + 1]
                    labeled(key, value, cells[cell_index] + "：" + value,
                            f"{locator}/rows/{row_index}/cells/{cell_index + 1}", bbox)
        plain = "".join(parser.outside)
        lines = [{"text": text, "locator": f"{locator}/lines/{i}"}
                 for i, text in enumerate(plain.splitlines()) if text.strip()]
        for line in lines:
            observed_text.append((line["text"], locator, bbox, block.get("type") == "doc_title" or bool(re.fullmatch(r"国有.*租赁合同", line["text"].strip()))))
        extracted, plain_diagnostics = parse_lines(lines, page, profile, response_id=response_id)
        diagnostics['observations'].extend(plain_diagnostics['observations'])
        diagnostics['parser_disagreements'].extend(plain_diagnostics['parser_disagreements'])
        for key, rows in extracted.items():
            if key == "doc_type":
                continue  # Choose document title/renewal observation consistently across blocks below.
            for item in rows:
                add(key, item["value"], (item["raw_text"], item.get("raw_value", item["value"])),
                    item.get('response_locator'), bbox if item.get('response_locator') else None,
                    text_role=item.get("text_role", "unspecified"))
    kind, type_source = document_type_observation([item[0] for item in observed_text])
    if kind:
        found = next(item for item in observed_text if item[0] == type_source)
        add("doc_type", kind, (found[0], found[0]), found[1], found[2])
    originals = {c["value"] for c in out["original_contract_no"]}
    out["contract_no"] = [c for c in out["contract_no"] if c["value"] not in originals]
    diagnostics["candidate_count"] = sum(len(rows) for rows in out.values())
    return out, diagnostics
