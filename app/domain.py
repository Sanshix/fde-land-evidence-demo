"""Deterministic exercise accounting. No OCR, network, or ground-truth reads."""

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

RULE_VERSION = "exercise-2026-09-30-v1"
AS_OF = date(2026, 9, 30)
FIELDS = {
    "contract_no": "合同编号",
    "contract_type": "合同类型",
    "tenant": "承租人",
    "identity": "证件号码",
    "phone": "联系电话",
    "parcel_no": "宗地号",
    "address": "坐落",
    "area": "土地面积",
    "use": "用途",
    "grade": "土地级别",
    "start": "租赁起始日",
    "end": "租赁终止日",
    "mode": "计租方式",
    "annual": "年租金小写",
    "annual_upper": "年租金大写",
    "signed": "签订日期",
}
REQUIRED = {"tenant", "parcel_no", "address", "area", "use", "start", "end", "mode"}


def dec(v):
    n = Decimal(str(v).replace(",", ""))
    if not n.is_finite():
        raise ValueError("金额/面积必须是有限数")
    return n


def money(v):
    return dec(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def clean(v):
    return re.sub(r"\s+", "", str(v or "")).translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def upper_money(text):
    """Parse common Chinese financial numerals; fail closed on unknown tokens."""
    t = clean(text).replace("人民币", "").replace("圆", "元").replace("正", "整")
    if not t:
        raise ValueError("大写金额缺失")
    digits = dict(zip("零壹贰叁肆伍陆柒捌玖", range(10), strict=True))
    units = {"拾": 10, "佰": 100, "仟": 1000}
    integer, _, fraction = t.partition("元")
    total = section = number = 0
    for c in integer:
        if c in digits:
            number = digits[c]
        elif c in units:
            section += (number or 1) * units[c]
            number = 0
        elif c in {"万", "亿"}:
            section += number
            number = 0
            if c == "万":
                total += section * 10000
            else:
                total = (total + section) * 100000000
            section = 0
        else:
            raise ValueError("大写金额含未知字符")
    result = Decimal(total + section + number)
    digit = 0
    for c in fraction:
        if c in digits:
            digit = digits[c]
        elif c in {"角", "分"}:
            result += Decimal(digit) / (10 if c == "角" else 100)
            digit = 0
        elif c != "整":
            raise ValueError("大写金额含未知字符")
    return money(result)


def validate_fields(f):
    errors = [f"{FIELDS[k]}缺失" for k in REQUIRED if not str(f.get(k, "")).strip()]
    if f.get("mode") not in {"固定", "随标准调整"}:
        errors.append("计租方式需人工明确选择")
    for key in ("start", "end"):
        try:
            date.fromisoformat(f.get(key, ""))
        except ValueError:
            errors.append(f"{FIELDS[key]}格式错误")
    if f.get("start") and f.get("end") and f["start"] > f["end"]:
        errors.append("租期起始晚于终止")
    try:
        if dec(f.get("area", "")) <= 0:
            errors.append("面积必须大于0")
    except Exception:
        errors.append("面积不是有效数字")
    if f.get("mode") == "固定":
        try:
            if dec(f.get("annual", "")) < 0:
                errors.append("年租金不得为负")
        except Exception:
            errors.append("固定计租需明确年租金")
    if f.get("mode") == "随标准调整" and f.get("grade") not in {"一级", "二级", "三级", "四级"}:
        errors.append("随标准调整需确认土地级别")
    if f.get("use") not in {"住宅", "商服", "工业", "仓储"}:
        errors.append("用途未映射到四类计租标准")
    return list(dict.fromkeys(errors))


def cadastre(v):
    t = clean(v)
    if len(t) == 28:
        parts = (t[6:9], t[9:12], t[14:19])
    else:
        parts = re.split(r"[-－—]", t)
    if len(parts) == 3 and all(x.isdigit() for x in parts):
        return tuple(int(x) for x in parts)
    return None


def match_registry(f, registry):
    key = cadastre(f.get("parcel_no"))
    return [r for r in registry if r.get("权利性质") == "租赁" and key and cadastre(r["不动产单元号"]) == key]


def compute(contracts, rates, payments=(), reliefs=(), as_of=AS_OF):
    """All inputs are human-approved. A contract is {'id','fields', optional stop}.

    Unresolved overlaps fail closed; callers must explicitly choose an evidence-based
    scenario with adjusted effective end dates. Future years are not billed.
    """
    if not contracts:
        return {
            "annual": [],
            "allocations": [],
            "due": "0.00",
            "paid": "0.00",
            "arrears": "0.00",
            "issues": [],
        }
    ordered = sorted(contracts, key=lambda c: (c["fields"]["start"], c["id"]))
    issues, lines, due = [], [], {}
    for i, contract in enumerate(ordered):
        f = contract["fields"]
        errors = validate_fields(f)
        if errors:
            raise ValueError("；".join(errors))
        start, end = date.fromisoformat(f["start"]), date.fromisoformat(f["end"])
        if i and start <= date.fromisoformat(ordered[i - 1].get("stop") or ordered[i - 1]["fields"]["end"]):
            raise ValueError("合同期间重叠：先列明并人工确认暂算方案")
        stop = contract.get("stop")
        if stop:
            end = min(end, date.fromisoformat(stop))
        elif i == len(ordered) - 1 and end < as_of:
            issues.append(
                {
                    "type": "合同到期未续",
                    "document": contract["id"],
                    "reason": "按末份合同继续计至基准日所在年度全年",
                }
            )
            end = date(as_of.year, 12, 31)
        end = min(end, date(as_of.year, 12, 31))
        fixed = money(f.get("annual", "0") or "0")
        if f.get("annual_upper"):
            upper = upper_money(f["annual_upper"])
            if upper != fixed:
                issues.append(
                    {
                        "type": "年租金大小写不一致",
                        "document": contract["id"],
                        "reason": f"小写{fixed}；大写{upper}，固定方式暂按大写",
                    }
                )
                fixed = upper
        for year in range(start.year, end.year + 1):
            months = (
                min(12, end.month if year == end.year else 12)
                - (start.month if year == start.year else 1)
                + 1
            )
            if months <= 0:
                continue
            rate, source = None, "合同固定年租金"
            if f["mode"] == "固定":
                annual = fixed
            else:
                available = [
                    r for r in rates if r["用途"] == f["use"] and str(r["执行起始日"])[:10] <= f"{year}-01-01"
                ]
                if not available:
                    raise ValueError(f"{year}无适用费率")
                rr = max(available, key=lambda r: str(r["执行起始日"]))
                rate = dec(rr[f["grade"]])
                source = f"05计租标准:{rr['执行起始日']}:{f['use']}:{f['grade']}"
                annual = money(dec(f["area"]) * rate)
            prorated = money(annual * months / 12)
            due[year] = due.get(year, Decimal(0)) + prorated
            lines.append(
                {
                    "contract": contract["id"],
                    "year": year,
                    "months": months,
                    "area": f["area"],
                    "rate": str(rate) if rate else None,
                    "annual": str(annual),
                    "prorated": str(prorated),
                    "source": source,
                }
            )
    for year in list(due):
        applicable = [r for r in reliefs if int(r["减免起始年度"]) <= year <= int(r["减免终止年度"])]
        if len(applicable) > 1:
            raise ValueError("同年多条减免需先确认叠加口径")
        if applicable:
            proportion = str(applicable[0]["减免比例"]).strip()
            ratio = dec(proportion.rstrip("%")) / (100 if proportion.endswith("%") else 1)
            if not 0 <= ratio <= 1:
                raise ValueError("减免比例错误")
            due[year] = money(due[year] * (1 - ratio))
    seen, unique = {}, []
    for p in payments:
        if str(p["缴费日期"])[:10] > as_of.isoformat():
            continue
        receipt = str(p["票据号"])
        signature = (
            str(p["金额（元）"]),
            str(p["缴费日期"]),
            str(p.get("所属年度") or ""),
            str(p.get("缴款人") or ""),
        )
        if receipt in seen:
            if seen[receipt] != signature:
                raise ValueError("同票据号内容冲突，禁止静默去重")
            issues.append({"type": "缴费记录异常", "reason": f"票据{receipt}重复，只计一次"})
            continue
        seen[receipt] = signature
        unique.append(p)
    allocated = {y: Decimal(0) for y in due}
    allocations, overpaid, total_paid = [], Decimal(0), Decimal(0)
    for p in sorted(unique, key=lambda x: (str(x["缴费日期"]), str(x["票据号"]))):
        amount = money(p["金额（元）"])
        if amount < 0:
            raise ValueError("负数缴款需单独核对退款规则")
        total_paid += amount
        remain = amount
        year = p.get("所属年度")
        years = (
            [int(year)]
            if year not in (None, "")
            else [y for y in sorted(due) if y <= int(str(p["缴费日期"])[:4])]
        )
        for y in years:
            part = min(remain, max(Decimal(0), due.get(y, Decimal(0)) - allocated.get(y, Decimal(0))))
            if part:
                allocated[y] = allocated.get(y, Decimal(0)) + part
                allocations.append({"receipt": str(p["票据号"]), "year": y, "amount": str(part)})
                remain -= part
        if remain:
            overpaid += remain
            allocations.append(
                {"receipt": str(p["票据号"]), "year": None, "amount": str(remain), "type": "多缴"}
            )
    annual_lines = [
        {
            "year": y,
            "due": str(money(due[y])),
            "paid": str(money(allocated[y])),
            "arrears": str(money(due[y] - allocated[y])),
        }
        for y in sorted(due)
    ]
    total_due = money(sum(due.values()))
    return {
        "ruleset": RULE_VERSION,
        "as_of": as_of.isoformat(),
        "segments": lines,
        "annual": annual_lines,
        "allocations": allocations,
        "due": str(total_due),
        "paid": str(money(total_paid)),
        "overpaid": str(money(overpaid)),
        "arrears": str(money(total_due - total_paid)),
        "issues": issues,
    }
