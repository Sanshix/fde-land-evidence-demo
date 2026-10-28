"""把纯文本 OCR 结果（逐行，可带坐标）抽成合同要素候选。

练习集合同是“第一条 甲方将位于……的国有土地（地号：……）……”式的条款正文，不是“标签：值”表格，
所以除标签匹配外还按条款句式抽取。只做原文抽取与格式规范，不推算、不补事实；结果一律待人工确认。
"""

import re

from .domain import FIELDS, clean, normalize_mode

CN = {**{str(i): i for i in range(10)}, "〇": 0, "○": 0, "O": 0, "o": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
CN_DATE = r"([〇○Oo零一二三四五六七八九0-9]{4})\s*年\s*([一二三四五六七八九十0-9]{1,3})\s*月\s*([一二三四五六七八九十0-9]{1,3})\s*日"


def _cn_int(text):
    text = text.strip()
    if text.isdigit():
        return int(text)
    if all(c in CN for c in text):
        return int("".join(str(CN[c]) for c in text))
    total, number = 0, 0
    for c in text:
        if c in CN:
            number = CN[c]
        elif c == "十":
            total += (number or 1) * 10
            number = 0
        else:
            raise ValueError(text)
    return total + number


def cn_date(match):
    from datetime import date

    try:
        return date(*(_cn_int(x) for x in match.groups())).isoformat()
    except (ValueError, TypeError):
        return None


PATTERNS = [
    ("contract_no", r"([×xX]{1,2}[一-龥]{1,6}[租变字]{1,2}[（(〔\[]\s*\d{4}\s*[)）〕\]]\s*第?\s*\d{1,4}\s*号)"),
    ("lessor", r"出租人[（(]?甲方[)）]?[:：]?\s*(\S+?局)"),
    ("tenant", r"承租人[（(]?乙方[)）]?[:：]?\s*(\S{2,60})$"),
    ("identity", r"(?:号码|执照号|信用代码)[)）]?[:：]?\s*([0-9A-Za-z]{15,20})"),
    ("residence", r"住\s*所[:：]\s*(\S+)"),
    ("phone", r"联系电话[:：]?\s*(\d{6,13})"),
    ("address", r"位[于十干千](\S+?)的国有土地"),
    ("parcel_no", r"(?:地号|宗地号|不动产单元号)[:：]\s*([0-9A-Z]{6,28}|\d{1,2}\s*[-－—]\s*\d{1,2}\s*[-－—]\s*\d{1,4})"),
    ("area", r"面积\s*([\d,]+(?:\.\d{1,2})?)\s*平方米"),
    ("use", r"用途为?\s*(住宅|商服|工业|仓储)"),
    ("grade", r"(?:土地级别|级别)[:：为]?\s*([一二三四]级)"),
    ("rate_standard", r"每平方米\s*([\d.]+)\s*元"),
    ("annual_upper", r"大写[:：]?\s*([零壹贰叁肆伍陆柒捌玖拾佰仟万元角分整参叁]+)"),
    ("signed", r"签订日期[:：]?\s*" + CN_DATE),
    ("original_contract_no", r"原合同[（(]?编号[:：]\s*(\S+?号)"),
]


LINE_KEYS = {"lessor", "tenant", "identity", "residence", "phone", "grade", "signed", "original_contract_no"}


def prose_candidates(lines, page_id, model, provider, kind="ocr"):
    """lines: [{'text', optional 'box'[x1,y1,x2,y2], optional 'score', 'locator'}]."""
    out = {k: [] for k in FIELDS}
    text = "".join(clean(line["text"]) for line in lines)

    def where(snippet):
        for line in lines:
            if snippet and snippet[:6] in clean(line["text"]):
                return line
        return lines[0] if lines else {}

    def add(key, value, raw, reason="条款句式抽取"):
        value = str(value).strip("：:;,，；。（）()")
        if not value or len(value) > 160 or any(c["value"] == value for c in out[key]):
            return
        line = where(raw)
        out[key].append(
            {
                "value": value,
                "raw_text": raw,
                "model": model,
                "provider": provider,
                "source_page_id": page_id,
                "bbox": line.get("box"),
                "response_locator": line.get("locator"),
                "score": line.get("score", 0),
                "score_meaning": "ocr_line_score" if "score" in line else "not_provided",
                "kind": kind,
                "reason": reason + "；须对照原件核验",
                "verified": False,
                "can_publish": False,
                "evidence_status": "line_region_needs_human" if line.get("box") else "missing_region",
            }
        )

    line_texts = [clean(line["text"]) for line in lines]
    for key, pattern in PATTERNS:
        haystacks = line_texts if key in LINE_KEYS else [text]
        for m in (m for hay in haystacks for m in re.finditer(pattern, hay)):
            if key == "signed":
                value = cn_date(m)
            elif key == "parcel_no":
                value = re.sub(r"\s", "", m.group(1))
            elif key == "annual_upper":
                value = m.group(1).replace("参", "叁")
            else:
                value = m.group(1)
            if value:
                add(key, value, m.group(0))
    term = re.search(CN_DATE + r"\s*起\s*至\s*" + CN_DATE, text)
    if term:
        dates = [cn_date(m) for m in re.finditer(CN_DATE, term.group(0))]
        if len(dates) == 2 and all(dates):
            add("start", dates[0], term.group(0))
            add("end", dates[1], term.group(0))
    rent = re.search(r"年租金为?人民币\s*((?:[\d,]+\.\d{2}\s*){1,3})元", text)
    if rent:
        numbers = re.findall(r"[\d,]+\.\d{2}", rent.group(1))
        for n in numbers:
            add("annual", n.replace(",", ""), rent.group(0))
        if len(numbers) > 1:
            add("altered_field", "年租金小写", rent.group(0), "同一位置出现多个金额，疑似划改")
            add("altered_before", numbers[0].replace(",", ""), rent.group(0), "疑似划改前的金额")
    if "不作调整" in text or re.search(r"[☑✓√■]\s*固定", text):
        add("mode", "固定", "合同期内年租金不作调整" if "不作调整" in text else "固定（勾选）")
    if re.search(r"[☑✓√■]\s*随", text) or re.search(r"随(?:基准地价|计租标准)调整", text) and "不作调整" not in text:
        add("mode", normalize_mode("随基准地价调整"), "随基准地价调整")
    if "变更协议" in text:
        add("doc_type", "变更", "变更协议")
    elif "续签" in text or "续期" in text:
        add("doc_type", "续期", "续签/续期")
    elif "租赁合同" in text:
        add("doc_type", "新签", "租赁合同（未见续签字样）", "未见续签/变更字样，按新签提示")
    if re.search(r"宗\s*地\s*图", text) and len(text) < 400:
        add("has_map", "是", "宗地图", "本页为宗地图页")
    originals = {c["value"] for c in out["original_contract_no"]}
    out["contract_no"] = [c for c in out["contract_no"] if c["value"] not in originals]
    return out


def merge(*groups):
    out = {k: [] for k in FIELDS}
    for group in groups:
        for key, rows in group.items():
            for row in rows:
                if not any(r["value"] == row["value"] and r.get("model") == row.get("model") for r in out[key]):
                    out[key].append(row)
    return out
