"""Business sources, parcel grouping, payment matching and the per-parcel calculation.

No standard-answer directory is read here. Amounts come only from domain.compute (rule program).
"""

import json
import os
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import openpyxl

from .domain import (
    ALTERABLE,
    GRADES,
    cadastre,
    clean,
    compute,
    dec,
    id_problem,
    match_registry,
    money,
    normalize_mode,
)
from .store import dumps, uid

CODE_ROOT = Path(__file__).resolve().parents[1]
REQUIRED_SOURCES = ("02_不动产登记导出.xlsx", "03_租金缴费记录.xlsx", "04_租金减免登记.xlsx", "05_计租标准与土地级别.xlsx")


def locate_samples():
    """Business base data directory (练习集 / 评测集 / 正式导出).

    FDE_SAMPLES_DIR wins. Otherwise look next to the code for the layouts teams actually use:
    ../练习集, ../国有土地租赁模拟样本包_练习集/练习集, ./练习集.
    """
    configured = os.environ.get("FDE_SAMPLES_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    candidates = [CODE_ROOT.parent / "练习集", CODE_ROOT / "练习集"]
    candidates += sorted(CODE_ROOT.parent.glob("*/练习集"))
    for candidate in candidates:
        if all((candidate / name).is_file() for name in REQUIRED_SOURCES):
            return candidate.resolve()
    return candidates[0]


SAMPLES = locate_samples()


def read_sheet(name, sheet=None):
    path = SAMPLES / name
    if not path.is_file():
        raise FileNotFoundError(f"缺少业务基础表 {path}；请设置 FDE_SAMPLES_DIR 指向练习集/评测集目录")
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = workbook[sheet] if sheet else workbook.active
    rows = list(ws.values)
    data = [dict(zip(rows[0], row, strict=True)) for row in rows[1:] if any(v is not None for v in row)]
    workbook.close()
    return data


def _text_rows(rows, keys):
    for row in rows:
        for key in keys:
            if row.get(key) is not None and not isinstance(row[key], str):
                value = row[key]
                row[key] = value.date().isoformat() if hasattr(value, "date") else str(value)
    return rows


def sources():
    levels = read_sheet("05_计租标准与土地级别.xlsx", "土地级别范围")
    return {
        "registry": _text_rows(read_sheet("02_不动产登记导出.xlsx"), ["不动产单元号", "证件号码", "联系电话"]),
        "payments": _text_rows(read_sheet("03_租金缴费记录.xlsx"), ["票据号", "缴费日期", "所属年度"]),
        "reliefs": read_sheet("04_租金减免登记.xlsx"),
        "rates": _text_rows(read_sheet("05_计租标准与土地级别.xlsx", "计租标准"), ["执行起始日"]),
        "levels": levels,
    }


# ---------------------------------------------------------------- 坐落归一（只用于匹配，不改原文）

CN_DIGITS = {"〇": 0, "○": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def chinese_number(text):
    if not text:
        return None
    if all(c in CN_DIGITS for c in text):
        return int("".join(str(CN_DIGITS[c]) for c in text))
    total, number = 0, 0
    for c in text:
        if c in CN_DIGITS:
            number = CN_DIGITS[c]
        elif c in "十百千":
            total += (number or 1) * {"十": 10, "百": 100, "千": 1000}[c]
            number = 0
        else:
            return None
    return total + number


def known_streets(source):
    out = {}
    for row in source.get("levels", []):
        for street in re.split(r"[、，,\s]+", str(row.get("所含街路（××镇）") or "")):
            if street:
                out[street] = row["土地级别"]
    return out


def address_key(s, streets=None):
    """Normalize 坐落 for matching only: 县/镇前缀、汉字门牌号、“院”字、“号”字、街/大街简写。"""
    text = clean(s)
    text = re.sub(r"^[×Xx＊*]*[县镇]", "", text)
    text = re.sub(
        r"[〇○零一二两三四五六七八九十百千]+(?=号|$)", lambda m: str(chinese_number(m.group()) or m.group()), text
    )
    text = text.replace("院", "")
    text = re.sub(r"号$", "", text)
    for street in sorted(streets or {}, key=len, reverse=True):
        short = street.replace("大街", "街")
        if short != street and text.startswith(short) and short not in (streets or {}):
            text = street + text[len(short) :]
            break
    return text


def street_grade(address, streets):
    text = address_key(address, streets)
    for street in sorted(streets, key=len, reverse=True):
        if text.startswith(street) or street in text:
            return streets[street]
    return None


# ---------------------------------------------------------------- 登记匹配、归宗

def registry_for(fields, source):
    """Return (registry_row_or_None, basis). Cadastre first; unique 坐落 match is a reviewed fallback."""
    found = match_registry(fields, source["registry"])
    if len(found) > 1:
        raise ValueError("多个登记候选，禁止自动归宗")
    if found:
        return found[0], "宗地号"
    streets = known_streets(source)
    key = address_key(fields.get("address"), streets)
    if key:
        rows = [
            r
            for r in source["registry"]
            if r.get("权利性质") == "租赁" and address_key(r["坐落"], streets) == key
        ]
        if len(rows) == 1:
            return rows[0], "坐落"
    return None, None


def parcel_key(f, source):
    """Business key for a parcel. Registered parcels use the 28-digit unit number; otherwise the
    normalized cadastre tuple so that old (7-3-100) and new (28-digit) numbers of one parcel agree."""
    row, _ = registry_for(f, source)
    if row:
        return row["不动产单元号"]
    key = cadastre(f.get("parcel_no"))
    if key:
        return "cadastre:" + "-".join(map(str, key))
    return "unregistered:" + (address_key(f.get("address"), known_streets(source)) or clean(f.get("parcel_no")))


def ensure_parcel(db, key):
    row = db.execute("SELECT id,merged_into FROM parcels WHERE business_key=?", (key,)).fetchone()
    if row:
        identifier = row[0]
        seen = set()
        while True:
            nxt = db.execute("SELECT merged_into FROM parcels WHERE id=?", (identifier,)).fetchone()
            if not nxt or not nxt[0] or nxt[0] in seen:
                return identifier
            seen.add(identifier)
            identifier = nxt[0]
    identifier = uid()
    db.execute("INSERT INTO parcels(id,business_key) VALUES(?,?)", (identifier, key))
    return identifier


def linked_parcels(db, fields, exclude=None):
    """Parcels already holding a document that this one cites (原合同编号) or that cites this one."""
    own, original = clean(fields.get("contract_no")), clean(fields.get("original_contract_no"))
    found = set()
    for row in db.execute(
        "SELECT id,parcel_id,review FROM documents WHERE state='published' AND parcel_id IS NOT NULL"
    ):
        if row["id"] == exclude or not row["review"]:
            continue
        f = json.loads(row["review"]).get("fields", {})
        if (original and clean(f.get("contract_no")) == original) or (
            own and clean(f.get("original_contract_no")) == own
        ):
            found.add(row["parcel_id"])
    return found


# ---------------------------------------------------------------- 缴费候选

def suggest(f, source, extra_fields=()):
    """Payment/relief candidates for one parcel with the basis shown to the reviewer.

    强依据：收费员录入的宗地号或坐落与本宗一致。弱依据：宗地栏空白、缴款人与本宗承租人/权利人同名。
    """
    all_fields = [f, *extra_fields]
    streets = known_streets(source)
    try:
        r, basis = registry_for(f, source)
    except ValueError:
        return {"registry": match_registry(f, source["registry"]), "payments": [], "reliefs": [], "ambiguous": True}
    keys = {cadastre(x.get("parcel_no")) for x in all_fields} - {None}
    addresses = {address_key(x.get("address"), streets) for x in all_fields} - {""}
    names = {clean(x.get("tenant")) for x in all_fields} - {""}
    if r:
        keys.add(cadastre(r["不动产单元号"]))
        addresses.add(address_key(r["坐落"], streets))
        names.add(clean(r["权利人"]))
    payments = []
    for p in source["payments"]:
        text = p.get("宗地（收费员录入）")
        reason = None
        if text:
            if cadastre(text) in keys:
                reason = "收费员录入地号一致"
            elif address_key(text, streets) in addresses:
                reason = "收费员录入坐落一致"
        elif clean(p.get("缴款人")) in names:
            reason = "宗地栏空白，缴款人与本宗承租人/权利人同名（弱依据，请核对）"
        if reason:
            strong = bool(text)
            payments.append({**p, "match_basis": reason, "match_strength": "strong" if strong else "weak",
                             "preselect": strong})
    reliefs = [
        x
        for x in source["reliefs"]
        if (r and x.get("不动产单元号") == r["不动产单元号"]) or address_key(x.get("坐落"), streets) in addresses
    ]
    return {
        "registry": [r] if r else [],
        "registry_basis": basis,
        "payments": payments,
        "reliefs": reliefs,
        "ambiguous": False,
        "warning": "候选按地号/坐落匹配，宗地栏空白的按缴款人同名列出；请核对后勾选，漏选或误选会直接影响欠缴。",
    }


# ---------------------------------------------------------------- 计算

def parse_date(text):
    t = clean(text)
    if not t:
        return None
    try:
        return date.fromisoformat(t).isoformat()
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{4})[年./-](\d{1,2})[月./-](\d{1,2})日?", t)
    if m:
        return date(*map(int, m.groups())).isoformat()
    m = re.fullmatch(r"([〇○零一二三四五六七八九]{4})年([一二三四五六七八九十]+)月([一二三四五六七八九十]+)日", t)
    if m:
        return date(*(chinese_number(x) for x in m.groups())).isoformat()
    return None


def completeness_hint(doc_type, page_count, has_map):
    """制式合同为 正文/签章页/宗地图 三页。少页且附图在册 → 多半缺签章页；仅作提示，由人核对原件。"""
    if clean(doc_type) == "变更" or not page_count:
        return None
    expected = 3 if clean(has_map) != "否" else 2
    if page_count < expected:
        return "第2页（签章页）" if page_count == expected - 1 else f"{expected - page_count}页"
    return None


def normalize_fields(fields):
    """人工采纳值的格式规范（不改语义）：日期转 ISO（含汉字日期）、去掉“平方米/元/,”、计租方式同义词。"""
    f = {k: (v.strip() if isinstance(v, str) else v) for k, v in dict(fields or {}).items()}
    if "contract_type" in f:
        legacy = f.pop("contract_type")
        if legacy and f.get("doc_type") and legacy != f["doc_type"]:
            raise ValueError("合同类型的新旧字段不一致，请重新核对")
        f["doc_type"] = f.get("doc_type") or legacy
    for key in ("start", "end", "signed"):
        if f.get(key):
            f[key] = parse_date(f[key]) or f[key]
    for key in ("area", "annual", "rate_standard"):
        if f.get(key):
            f[key] = re.sub(r"(平方米|㎡|元|,|，)", "", f[key])
    if f.get("mode"):
        f["mode"] = normalize_mode(f["mode"]) or f["mode"]
    return f


def _is_contract(fields):
    return clean(fields.get("doc_type")) != "变更"


def _order_key(doc):
    f = doc["review"]["fields"]
    return (f.get("start") or f.get("signed") or "", f.get("signed") or "", doc["id"])


def resolve_contract(doc, grade_address, streets):
    f = dict(doc["review"]["fields"])
    f["mode"] = normalize_mode(f.get("mode"))
    grade_basis = "合同载明"
    if f.get("grade") not in GRADES:
        f["grade"] = street_grade(grade_address, streets) or street_grade(f.get("address"), streets) or ""
        grade_basis = "按坐落街路确定（口径第二条）" if f["grade"] else "未能确定"
    return {"id": doc["id"], "fields": f, "stop": doc["review"].get("stop") or None, "grade_basis": grade_basis}


def uncertainty_factors(contracts):
    """口径第十二条：列出说得通的算法。每个因素是互斥选项，后续逐一选应缴最少者。"""
    factors = []
    for c in contracts:
        f = c["fields"]
        if f["mode"] == "未勾选":
            factors.append(
                {
                    "document": c["id"],
                    "what": "计租方式未勾选",
                    "options": [("按固定", {c["id"]: {"mode": "固定"}}), ("按随基准地价调整", {c["id"]: {"mode": "随标准调整"}})],
                }
            )
        target = ALTERABLE.get(clean(f.get("altered_field")))
        if target and clean(f.get("altered_sealed")) == "否" and target in {"area", "annual", "annual_upper", "start", "end", "mode"}:
            before = clean(f.get("altered_before"))
            if target in {"start", "end"}:
                before = parse_date(before) or before
            if target == "mode":
                before = normalize_mode(before)
            if before and before != clean(f.get(target)):
                factors.append(
                    {
                        "document": c["id"],
                        "what": "涂改处无手印或盖章",
                        "options": [("按涂改前", {c["id"]: {target: before}}), ("按涂改后", {c["id"]: {}})],
                    }
                )
    ordered = sorted(contracts, key=lambda c: (c["fields"]["start"], c["id"]))
    for old, new in zip(ordered, ordered[1:], strict=False):
        if old.get("stop"):
            continue
        if new["fields"]["start"] <= old["fields"]["end"]:
            later = max((old, new), key=lambda c: (c["fields"].get("signed") or "", c["fields"]["start"]))
            factors.append(
                {
                    "document": later["id"],
                    "what": "同一宗地两份合同期间重叠",
                    "options": [
                        (
                            "旧合同自新合同起始日前终止",
                            {old["id"]: {"__stop": (date.fromisoformat(new["fields"]["start"]) - timedelta(days=1)).isoformat()}},
                        ),
                        (
                            "以旧合同为准，新合同自旧合同期满后生效",
                            {new["id"]: {"start": (date.fromisoformat(old["fields"]["end"]) + timedelta(days=1)).isoformat()}},
                        ),
                    ],
                }
            )
    return factors


def _apply(contracts, choices):
    out = []
    for c in contracts:
        f, stop = dict(c["fields"]), c.get("stop")
        for patch in choices:
            for key, value in patch.get(c["id"], {}).items():
                if key == "__stop":
                    stop = value
                else:
                    f[key] = value
        out.append({"id": c["id"], "fields": f, "stop": stop})
    return out


def _run(contracts, factors, picks, source, payments, reliefs):
    choices = [factor["options"][i][1] for factor, i in zip(factors, picks, strict=True)]
    # 新合同整段落在旧合同期内时，“新合同自旧合同期满后生效”会让起始晚于终止：该合同在此算法下不计租
    applied = [c for c in _apply(contracts, choices) if c["fields"]["start"] <= c["fields"]["end"]]
    return compute(applied, source["rates"], payments, reliefs)


def calculate_documents(docs, source):
    if not docs:
        return {**compute([], source["rates"]), "documents": [], "recipient": None, "phone": None, "issues": []}
    for doc in docs:
        review = doc["review"]
        if not review or not review.get("evidence_clear"):
            raise ValueError("包含未完成原件核对的资料")
    streets = known_streets(source)
    regs = {}
    basis = {}
    for doc in docs:
        row, how = registry_for(doc["review"]["fields"], source)
        if row:
            regs[row["不动产单元号"]] = row
            basis[doc["id"]] = how
    if len(regs) > 1:
        raise ValueError("同宗材料对应多个不动产单元，需人工拆分或确认归宗")
    reg = next(iter(regs.values()), None)
    contract_docs = sorted([d for d in docs if _is_contract(d["review"]["fields"])], key=_order_key)
    change_docs = sorted([d for d in docs if not _is_contract(d["review"]["fields"])], key=_order_key)
    contracts = [resolve_contract(d, reg["坐落"] if reg else None, streets) for d in contract_docs]
    for c in contracts:
        if normalize_mode(c["fields"]["mode"]) in {"随标准调整", "未勾选"} and c["fields"]["grade"] not in GRADES:
            raise ValueError("无法确定土地级别：合同未载明，坐落街路也不在土地级别范围表内")
    selected_payments, selected_reliefs = {}, {}
    for doc in docs:
        review = doc["review"]
        wanted = {str(x) for x in review.get("payment_ids", [])}
        for p in source["payments"]:
            if str(p["序号"]) in wanted:
                selected_payments[str(p["序号"])] = p
        for r in source["reliefs"]:
            if r["批准文号"] in review.get("relief_ids", []):
                selected_reliefs[r["批准文号"]] = r
    payments, reliefs = list(selected_payments.values()), list(selected_reliefs.values())

    factors = uncertainty_factors(contracts)
    if len(factors) > 6:
        raise ValueError("存疑因素超过6个，请人工拆分确认")
    picks = [0] * len(factors)
    deltas = {}
    for _ in range(2):  # 每个因素取应缴最少的算法；两轮保证与其他因素的选择一致
        for i, factor in enumerate(factors):
            totals = []
            for option in range(len(factor["options"])):
                trial = picks[:]
                trial[i] = option
                totals.append(dec(_run(contracts, factors, trial, source, payments, reliefs)["due"]))
            picks[i] = min(range(len(totals)), key=lambda k: (totals[k], k))
            deltas[i] = (totals, money(max(totals) - min(totals)))
    result = _run(contracts, factors, picks, source, payments, reliefs)
    chosen = _apply(contracts, [f["options"][i][1] for f, i in zip(factors, picks, strict=True)])
    chosen_by_id = {c["id"]: c for c in chosen}

    key_doc = contract_docs[-1] if contract_docs else change_docs[-1]
    key_id = key_doc["id"]
    issues = []

    def add(kind, document, reason):
        issues.append({"type": kind, "document": document, "reason": reason})

    for factor_index, factor in enumerate(factors):
        totals, delta = deltas[factor_index]
        chosen_label = factor["options"][picks[factor_index]][0]
        others = "、".join(label for k, (label, _) in enumerate(factor["options"]) if k != picks[factor_index])
        add(
            "存疑待确认",
            factor["document"],
            f"{factor['what']}；暂按“{chosen_label}”计算，与“{others}”相差{delta}元",
        )
    for conflict in result.get("conflicts", []):
        add(
            "存疑待确认",
            key_id,
            f"票据{conflict['receipt']}重复登记且金额不一致；暂按金额较大的{conflict['kept']}元计，相差{conflict['difference']}元",
        )
    for doc in docs:
        r, f = doc["review"], doc["review"]["fields"]
        if r.get("uncertainty"):
            add("存疑待确认", doc["id"], r["uncertainty"])
        if f.get("altered_field"):
            add(
                "关键字段有涂改",
                doc["id"],
                f"{re.sub(r'(小写|大写)$', '', clean(f['altered_field']))}处有涂改" + ("，涂改处无签章" if clean(f.get("altered_sealed")) == "否" else ""),
            )
        missing = clean(f.get("missing_pages"))
        if missing and missing not in {"无", "否", "完整", "不缺"}:
            add("合同缺页", doc["id"], f"缺{missing}" if not missing.startswith("缺") else missing)
        if _is_contract(f) and clean(f.get("has_map")) == "否":
            add("缺宗地图", doc["id"], "未附宗地图")
        problem = id_problem(f.get("identity"))
        if problem:
            add("证件号码异常", doc["id"], problem)
        phone = clean(f.get("phone"))
        if not phone:
            add("联系电话缺失或无效", doc["id"], "未填联系电话")
        elif not re.fullmatch(r"\d{11}", phone):
            add("联系电话缺失或无效", doc["id"], f"联系电话“{phone}”位数不对")
        if reg:
            if f.get("address") and clean(f["address"]) != clean(reg["坐落"]):
                add("坐落或宗地号与登记不一致", doc["id"], f"坐落写作“{f['address']}”")
            elif cadastre(f.get("parcel_no")) != cadastre(reg["不动产单元号"]):
                add("坐落或宗地号与登记不一致", doc["id"], f"宗地号写作“{f.get('parcel_no')}”")
            if _is_contract(f) and f.get("area") and reg.get("宗地面积（㎡）") not in (None, ""):
                if dec(f["area"]) != dec(reg["宗地面积（㎡）"]):
                    add("面积与登记不一致", doc["id"], f"合同面积{f['area']}平方米，登记面积{money(reg['宗地面积（㎡）'])}平方米")
    for item in result["issues"]:
        add(item["type"], item.get("document") or key_id, item["reason"])
    tenants = {clean(d["review"]["fields"].get("tenant")) for d in docs}
    holder = clean(reg["权利人"]) if reg else ""
    payers = {clean(p.get("缴款人")) for p in payments}
    if payers - tenants - {holder, ""}:
        add("缴费记录异常", key_id, "缴款人不是承租人或登记权利人")
    latest_contract_tenant = contract_docs[-1]["review"]["fields"].get("tenant") if contract_docs else ""
    if reg and contract_docs and not change_docs and holder != clean(latest_contract_tenant):
        add("承租人已变更未办合同变更", key_id, f"登记权利人为{reg['权利人']}，合同承租人为{latest_contract_tenant}")
    if not reg:
        add("登记数据中无对应宗地", key_id, "不动产登记导出中查不到该宗地")

    # 同一档案号、同一问题类型合并为一行（问题清单按“档案号+类型”计）
    merged = {}
    for item in issues:
        k = (item["document"], item["type"])
        if k in merged:
            if item["reason"] not in merged[k]["reason"]:
                merged[k]["reason"] += "；" + item["reason"]
        else:
            merged[k] = dict(item)
    issues = list(merged.values())

    # 口径第九条：催缴对象与联系电话
    newest = sorted(docs, key=lambda d: (d["review"]["fields"].get("signed") or d["review"]["fields"].get("start") or "", d["id"]))
    recipient = reg["权利人"] if reg else newest[-1]["review"]["fields"]["tenant"]
    phone = clean(reg.get("联系电话")) if reg else ""
    if not phone:
        for d in reversed(newest):
            f = d["review"]["fields"]
            if clean(f.get("tenant")) == clean(recipient) and re.fullmatch(r"\d{11}", clean(f.get("phone"))):
                phone = clean(f["phone"])
                break
    key_contract = chosen_by_id.get(key_id)
    kf = key_contract["fields"] if key_contract else key_doc["review"]["fields"]
    key_resolved = next((c for c in contracts if c["id"] == key_id), None)
    arrears_years = [str(r["year"]) for r in result["annual"] if dec(r["arrears"]) > 0]
    return {
        **result,
        "issues": issues,
        "rate_version": source.get("rate_version", "source-unversioned"),
        "documents": sorted(d["id"] for d in docs),
        "recipient": recipient,
        "phone": phone,
        "registry_unit": reg["不动产单元号"] if reg else "",
        "registry_basis": sorted(set(basis.values())),
        "address": reg["坐落"] if reg else kf.get("address"),
        "latest_document": key_id,
        "ledger": {
            "登记坐落": reg["坐落"] if reg else "",
            "登记面积": str(money(reg["宗地面积（㎡）"])) if reg else "",
            "合同面积": key_doc["review"]["fields"].get("area", ""),
            "用途": kf.get("use", ""),
            "土地级别": kf.get("grade", ""),
            "土地级别依据": key_resolved["grade_basis"] if key_resolved else "",
            "计租方式": kf.get("mode", ""),
            "合同状态": result["status"],
            "合同终止日": key_doc["review"]["fields"].get("end", ""),
            "合同承租人": latest_contract_tenant,
            "登记权利人": reg["权利人"] if reg else "",
            "催缴对象": recipient,
            "联系电话": phone,
            "2026年度应缴": next((r["due"] for r in result["annual"] if r["year"] == 2026), "0.00"),
            "累计应缴": result["due"],
            "累计实缴": result["paid"],
            "欠缴金额": result["arrears"],
            "欠缴年度": "、".join(arrears_years),
        },
        "scenarios": [
            {
                "document": factor["document"],
                "what": factor["what"],
                "chosen": factor["options"][picks[i]][0],
                "options": [
                    {"label": label, "total_due": str(total)}
                    for (label, _), total in zip(factor["options"], deltas[i][0], strict=True)
                ],
                "difference": str(deltas[i][1]),
            }
            for i, factor in enumerate(factors)
        ],
        "source_manifest": dumps({"payments": sorted(selected_payments), "reliefs": sorted(selected_reliefs)}),
        "coverage": "口径第十一条13类问题按已确认字段自动判定；文书层面的涂改/缺页/附图依赖人工核对原件填写",
    }


def total_paid(payments):
    return money(sum((dec(p["金额（元）"]) for p in payments), Decimal(0)))
