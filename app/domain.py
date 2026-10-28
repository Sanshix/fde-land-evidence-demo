"""Deterministic exercise accounting. No OCR, network, or ground-truth reads.

Rules follow 练习集/06_计租标准与核算口径.docx (口径). Section numbers in comments refer to that file.
"""

import os
import re
from datetime import date
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal

RULE_VERSION = "exercise-2026-09-30-v2"
AS_OF = date(2026, 9, 30)
# 字段顺序即复核界面顺序。前 15 项为计租关键要素，其余为文书层面的问题判定要素（口径第十一条）。
FIELDS = {
    "doc_type": "合同类型",
    "contract_no": "合同编号",
    "original_contract_no": "原合同编号",
    "signed": "签订日期",
    "lessor": "出租人",
    "tenant": "承租人",
    "identity": "证件号码",
    "phone": "联系电话",
    "residence": "住所",
    "parcel_no": "宗地号",
    "address": "坐落",
    "area": "土地面积",
    "use": "用途",
    "grade": "土地级别",
    "start": "租赁起始日",
    "end": "租赁终止日",
    "mode": "计租方式",
    "rate_standard": "年租金标准",
    "annual": "年租金小写",
    "annual_upper": "年租金大写",
    "has_map": "有无宗地图",
    "missing_pages": "缺页情况",
    "altered_field": "涂改字段",
    "altered_before": "涂改前内容",
    "altered_sealed": "涂改处有无签章",
    "note": "手写批注",
}
# 文书层面的属性：判断“有/无/缺”本身不一定有可圈定的原文位置，不强制逐字段引用页。
DOCUMENT_ATTRIBUTES = {"doc_type", "has_map", "missing_pages", "altered_sealed", "note"}
DOC_TYPES = {"新签", "续期", "变更"}
MODES = {"固定", "随标准调整", "未勾选"}
USES = {"住宅", "商服", "工业", "仓储"}
GRADES = {"一级", "二级", "三级", "四级"}
REQUIRED = {"tenant", "parcel_no", "address", "area", "use", "start", "end", "mode"}
CHANGE_REQUIRED = {"tenant", "parcel_no"}
# 口径第四条（四）：可能影响金额的涂改字段 → 内部字段名
ALTERABLE = {
    "土地面积": "area",
    "面积": "area",
    "年租金": "annual",
    "年租金小写": "annual",
    "年租金大写": "annual_upper",
    "租赁终止日": "end",
    "终止日期": "end",
    "租赁起始日": "start",
    "起始日期": "start",
    "计租方式": "mode",
    "联系电话": "phone",
}


def rounding_mode():
    """口径写的是“四舍五入到分”（默认 half_up）。

    练习集标准答案实际用二进制浮点 round()，在 x.xx5 恰好一半时会舍去（如 1999.125→1999.12）。
    FDE_ROUNDING=answer_key 可复现标准答案，供自测比对；正式口径需主管部门书面确认。
    """
    return os.environ.get("FDE_ROUNDING", "half_up")


def dec(v):
    try:
        n = Decimal(str(v).replace(",", "").strip())
    except ArithmeticError as e:  # decimal.InvalidOperation 不是 ValueError，统一转为可提示的业务错误
        raise ValueError(f"不是有效数字：{v!r}") from e
    if not n.is_finite():
        raise ValueError("金额/面积必须是有限数")
    return n


def money(v):
    return dec(v).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def cents(value):
    """Rounding used for annual rent, proration and relief (口径三、四、六)."""
    if rounding_mode() == "answer_key":
        return Decimal(str(round(float(value), 2))).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    return money(value)


def clean(v):
    return re.sub(r"\s+", "", str(v or "")).translate(str.maketrans("０１２３４５６７８９", "0123456789"))


def normalize_mode(v):
    t = clean(v)
    if not t:
        return ""
    if t in {"未勾选", "未选择", "未注明", "未勾"}:
        return "未勾选"
    if "固定" in t and ("随" in t or "调整" in t):
        return ""  # 两个选项都出现：不是选择，交人工
    if "固定" in t or "不作调整" in t:
        return "固定"
    if t.startswith("随") or "基准地价" in t or "随标准" in t:
        return "随标准调整"
    return t


def upper_money(text):
    """Parse common Chinese financial numerals; fail closed on unknown tokens."""
    t = clean(text).replace("人民币", "").replace("圆", "元").replace("正", "整")
    if not t:
        raise ValueError("大写金额缺失")
    digits = dict(zip("零壹贰叁肆伍陆柒捌玖", range(10), strict=True))
    units = {"拾": 10, "佰": 100, "仟": 1000}
    if "元" not in t and re.search("[角分]", t):
        integer, fraction = "", t  # 不足一元的写法：伍角/伍分
    else:
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


def to_upper_money(amount):
    """Render an amount as 人民币大写 for notices (rule program, not a model)."""
    value = money(amount)
    if value < 0:
        raise ValueError("负数金额不生成大写")
    digits = "零壹贰叁肆伍陆柒捌玖"
    integer = int(value)
    jiao, fen = int(value * 10) % 10, int(value * 100) % 10
    if integer == 0 and not jiao and not fen:
        return "零元整"

    def four(n):
        out, zero = "", False
        for unit, base in (("仟", 1000), ("佰", 100), ("拾", 10), ("", 1)):
            d = n // base % 10
            if d:
                if zero and out:
                    out += "零"
                out += digits[d] + unit
                zero = False
            else:
                zero = True
        return out

    parts, text = [], ""
    for unit in ("", "万", "亿"):
        parts.append((integer % 10000, unit))
        integer //= 10000
        if not integer:
            break
    for n, unit in reversed(parts):
        if n:
            if text and n < 1000:
                text += "零"
            text += four(n) + unit
    text = (text + "元") if text else ""
    if jiao:
        text += digits[jiao] + "角"
    elif fen and text:
        text += "零"
    if fen:
        text += digits[fen] + "分"
    return text if (jiao or fen) else text + "整"


def id_problem(value):
    """口径第十一条：个人证件为 15 位旧号码，或 18 位校验位不对。单位统一信用代码不校验。"""
    t = clean(value).upper()
    if re.fullmatch(r"\d{15}", t):
        return f"证件号码为15位旧号码“{t}”"
    if re.fullmatch(r"\d{17}[\dX]", t):
        weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
        check = "10X98765432"[sum(int(a) * b for a, b in zip(t[:17], weights, strict=True)) % 11]
        if check != t[-1]:
            return f"证件号码“{t}”校验位不对"
    return None


def tenant_kind(identity):
    t = clean(identity).upper()
    return "个人" if re.fullmatch(r"\d{15}|\d{17}[\dX]", t) else ("单位" if t else "")


def validate_fields(f):
    doc_type = clean(f.get("doc_type"))
    if doc_type and doc_type not in DOC_TYPES:
        return ["合同类型只能是新签、续期或变更"]
    if doc_type == "变更":
        # 口径第十一条：变更协议不附宗地图、不载租金要素；只要求主体和宗地可归属。
        errors = [f"{FIELDS[k]}缺失" for k in sorted(CHANGE_REQUIRED) if not str(f.get(k, "")).strip()]
        if f.get("signed"):
            try:
                date.fromisoformat(f["signed"])
            except ValueError:
                errors.append("签订日期格式错误")
        return errors
    errors = [f"{FIELDS[k]}缺失" for k in sorted(REQUIRED) if not str(f.get(k, "")).strip()]
    mode = normalize_mode(f.get("mode"))
    if mode not in MODES:
        errors.append("计租方式需人工明确选择（原件未勾选请填“未勾选”，按存疑事项双算）")
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
    if mode in {"固定", "未勾选"}:
        try:
            if dec(f.get("annual", "")) < 0:
                errors.append("年租金不得为负")
        except Exception:
            errors.append("固定计租需明确年租金")
    if f.get("grade") and f.get("grade") not in GRADES:
        errors.append("土地级别只能是一级至四级；合同未载明请留空，由登记坐落按街路确定")
    if f.get("use") not in USES:
        errors.append("用途未映射到四类计租标准")
    if f.get("altered_field") and clean(f.get("altered_sealed")) not in {"是", "否"}:
        errors.append("有涂改时须注明涂改处有无手印或盖章（是/否）")
    return list(dict.fromkeys(errors))


def cadastre(v):
    """口径第十条：旧地号“地籍区-地籍子区-宗地号”与 28 位不动产单元号对应，前导零可省略。"""
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


def _year(value):
    """所属年度只认单一年度；写成“2003至2005”等多个年度时按未注明年度处理（口径七（三））。"""
    years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", str(value or ""))
    return int(years[0]) if len(set(years)) == 1 else None


def compute(contracts, rates, payments=(), reliefs=(), as_of=AS_OF):
    """All inputs are human-approved. A contract is {'id','fields', optional 'stop'}.

    fields must already carry a concrete grade/mode (business.resolve_contract does that).
    Unresolved overlaps fail closed here; business.calculate_documents enumerates the 存疑 scenarios.
    Future years are not billed (口径五：计至核算基准日所在年度).
    """
    empty = {
        "ruleset": RULE_VERSION,
        "as_of": as_of.isoformat(),
        "segments": [],
        "annual": [],
        "allocations": [],
        "due": "0.00",
        "paid": "0.00",
        "overpaid": "0.00",
        "arrears": "0.00",
        "issues": [],
        "status": "无有效合同",
        "last_end": None,
        "conflicts": [],
    }
    issues, lines, due = [], [], {}
    ordered = sorted(contracts, key=lambda c: (c["fields"]["start"], c["id"]))
    status, last_end = "无有效合同", None
    for i, contract in enumerate(ordered):
        f = contract["fields"]
        errors = validate_fields(f)
        if errors:
            raise ValueError("；".join(errors))
        mode = normalize_mode(f["mode"])
        if mode not in {"固定", "随标准调整"}:
            raise ValueError("计租方式未定，需按存疑事项选定暂算方案")
        start, end = date.fromisoformat(f["start"]), date.fromisoformat(f["end"])
        if i:
            previous = ordered[i - 1]
            if start <= date.fromisoformat(previous.get("stop") or previous["fields"]["end"]):
                raise ValueError("合同期间重叠：先列明并人工确认暂算方案")
        stop = contract.get("stop")
        last_end = f["end"]
        if stop:
            end = min(end, date.fromisoformat(stop))
            status = "已解除/收回"
        elif i == len(ordered) - 1 and end < as_of:
            # 口径五：期满未续、也未收回的，按末份合同计租方式继续计至基准日所在年度全年。
            issues.append(
                {
                    "type": "合同到期未续",
                    "document": contract["id"],
                    "reason": f"合同已于{f['end']}到期，未见续期合同",
                }
            )
            end = date(as_of.year, 12, 31)
            status = "到期未续"
        else:
            status = "有效"
        end = min(end, date(as_of.year, 12, 31))
        fixed = money(f.get("annual", "0") or "0")
        if f.get("annual_upper"):
            upper = upper_money(f["annual_upper"])
            if upper != fixed:
                issues.append(
                    {
                        "type": "年租金大小写不一致",
                        "document": contract["id"],
                        "reason": f"小写{fixed}元，大写{clean(f['annual_upper'])}",
                    }
                )
                fixed = upper  # 口径四（一）：暂按大写
        for year in range(start.year, end.year + 1):
            months = (end.month if year == end.year else 12) - (start.month if year == start.year else 1) + 1
            if months <= 0:
                continue
            rate, source = None, "合同固定年租金"
            if mode == "固定":
                annual = fixed
            else:
                grade = f.get("grade")
                if grade not in GRADES:
                    raise ValueError("随标准调整需确定土地级别")
                available = [
                    r for r in rates if r["用途"] == f["use"] and str(r["执行起始日"])[:10] <= f"{year}-01-01"
                ]
                if not available:
                    raise ValueError(f"{year}无适用费率")
                rr = max(available, key=lambda r: str(r["执行起始日"]))
                rate = dec(rr[grade])
                source = f"05计租标准:{str(rr['执行起始日'])[:10]}:{f['use']}:{grade}"
                annual = cents(dec(f["area"]) * rate)
            prorated = annual if months == 12 else cents(annual * months / 12)
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
    if not ordered:
        due = {}
    for year in list(due):
        applicable = [r for r in reliefs if int(r["减免起始年度"]) <= year <= int(r["减免终止年度"])]
        if len(applicable) > 1:
            raise ValueError("同年多条减免需先确认叠加口径")
        if applicable:
            proportion = str(applicable[0]["减免比例"]).strip()
            ratio = dec(proportion.rstrip("%")) / (100 if proportion.endswith("%") else 1)
            if not 0 <= ratio <= 1:
                raise ValueError("减免比例错误")
            due[year] = cents(due[year] * (1 - ratio))
    # 口径七（一）：同一票据号重复登记只计一次。内容不一致时按对产权人有利（金额较大）暂计并列存疑。
    by_receipt, duplicates, conflicts = {}, [], []
    for p in payments:
        receipt = str(p["票据号"]).strip()
        if receipt in by_receipt:
            kept = by_receipt[receipt]
            same = (money(kept["金额（元）"]), str(kept["缴费日期"])[:10], str(kept.get("所属年度") or "")) == (
                money(p["金额（元）"]),
                str(p["缴费日期"])[:10],
                str(p.get("所属年度") or ""),
            )
            duplicates.append(receipt)
            if not same:
                larger = max(kept, p, key=lambda x: money(x["金额（元）"]))
                conflicts.append(
                    {
                        "receipt": receipt,
                        "kept": str(money(larger["金额（元）"])),
                        "difference": str(abs(money(kept["金额（元）"]) - money(p["金额（元）"]))),
                    }
                )
                by_receipt[receipt] = larger
            continue
        by_receipt[receipt] = p
    unique = list(by_receipt.values())
    allocated = {y: Decimal(0) for y in due}
    shown_paid = {y: Decimal(0) for y in due}
    annotated = {}
    allocations, overpaid, total_paid = [], Decimal(0), Decimal(0)
    for p in sorted(unique, key=lambda x: (str(x["缴费日期"])[:10], str(x["票据号"]))):
        amount = money(p["金额（元）"])
        if amount < 0:
            raise ValueError("负数缴款需单独核对退款规则")
        total_paid += amount
        remain = amount
        year = _year(p.get("所属年度"))
        note = "缴费日期晚于核算基准日，按实缴计入" if str(p["缴费日期"])[:10] > as_of.isoformat() else None
        if year is not None:
            # 口径七（二）：注明年度的只记该年度，超出部分为多缴，不冲抵其他年度。
            annotated[year] = annotated.get(year, Decimal(0)) + amount
            years = [year]
        else:
            # 口径七（三）：冲抵当时为止尚未缴足的最早年度，依次向后（不以缴费日期截断）。
            years = sorted(due)
        for y in years:
            part = min(remain, max(Decimal(0), due.get(y, Decimal(0)) - allocated.get(y, Decimal(0))))
            if part:
                allocated[y] = allocated.get(y, Decimal(0)) + part
                shown_paid[y] = shown_paid.get(y, Decimal(0)) + part
                allocations.append({"receipt": str(p["票据号"]), "year": y, "amount": str(part), "note": note})
                remain -= part
        if remain:
            overpaid += remain
            if year is not None and year in shown_paid:
                shown_paid[year] += remain  # 逐年明细中“实缴”含该年度多缴
            allocations.append(
                {"receipt": str(p["票据号"]), "year": year, "amount": str(remain), "type": "多缴", "note": note}
            )
    if duplicates:
        issues.append({"type": "缴费记录异常", "reason": "票据号重复登记"})
    # 容差 0.01 元：只因“四舍五入”口径不同（见 rounding_mode）产生的一分钱差额不算缴费异常。
    mismatched = [y for y in sorted(annotated) if y in due and abs(annotated[y] - due[y]) > Decimal("0.01")]
    if mismatched:
        issues.append(
            {
                "type": "缴费记录异常",
                "reason": f"按年度缴费金额与应缴不符（{'、'.join(map(str, mismatched))}年度）",
            }
        )
    annual_lines = [
        {
            "year": y,
            "due": str(money(due[y])),
            "paid": str(money(shown_paid[y])),
            "arrears": str(money(max(Decimal(0), due[y] - allocated[y]))),
        }
        for y in sorted(due)
    ]
    total_due = money(sum(due.values(), Decimal(0)))
    return {
        **empty,
        "segments": lines,
        "annual": annual_lines,
        "allocations": allocations,
        "due": str(total_due),
        "paid": str(money(total_paid)),
        "overpaid": str(money(overpaid)),
        "arrears": str(money(total_due - total_paid)),
        "issues": issues,
        "status": status,
        "last_end": last_end,
        "conflicts": conflicts,
    }
