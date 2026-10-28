"""Business sources only. No standard answer directory is read here."""

import re
from pathlib import Path

import openpyxl

from .domain import clean, compute, dec, match_registry
from .store import dumps, uid

SAMPLES = Path(__file__).resolve().parents[2] / "练习集"


def read_sheet(name, sheet=None):
    workbook = openpyxl.load_workbook(SAMPLES / name, read_only=True, data_only=True)
    ws = workbook[sheet] if sheet else workbook.active
    rows = list(ws.values)
    data = [dict(zip(rows[0], row, strict=True)) for row in rows[1:] if any(v is not None for v in row)]
    workbook.close()
    return data


def sources():
    return {
        "registry": read_sheet("02_不动产登记导出.xlsx"),
        "payments": read_sheet("03_租金缴费记录.xlsx"),
        "reliefs": read_sheet("04_租金减免登记.xlsx"),
        "rates": read_sheet("05_计租标准与土地级别.xlsx", "计租标准"),
    }


def address_key(s):
    text = clean(s)
    text = re.sub(r"^[×Xx＊*]+[县镇]", "", text)
    return text


def suggest(f, source):
    registry = match_registry(f, source["registry"])
    if len(registry) > 1:
        return {"registry": registry, "payments": [], "reliefs": [], "ambiguous": True}
    r = registry[0] if registry else None
    addresses = {address_key(f.get("address"))}
    if r:
        addresses.add(address_key(r["坐落"]))
    addresses.discard("")
    payments = [p for p in source["payments"] if address_key(p["宗地（收费员录入）"]) in addresses]
    reliefs = [x for x in source["reliefs"] if r and x["不动产单元号"] == r["不动产单元号"]]
    return {
        "registry": registry,
        "payments": payments,
        "reliefs": reliefs,
        "ambiguous": False,
        "warning": "仅列精确号码/坐落候选；需人工核对是否漏缴费或误关联。姓名不作为独立归宗依据。",
    }


def parcel_key(f, source):
    found = match_registry(f, source["registry"])
    if len(found) > 1:
        raise ValueError("多个登记候选，禁止自动归宗")
    return found[0]["不动产单元号"] if found else "unregistered:" + clean(f["parcel_no"])


def ensure_parcel(db, key):
    row = db.execute("SELECT id FROM parcels WHERE business_key=?", (key,)).fetchone()
    if row:
        return row[0]
    identifier = uid()
    db.execute("INSERT INTO parcels(id,business_key) VALUES(?,?)", (identifier, key))
    return identifier


def calculate_documents(docs, source):
    if not docs:
        return {**compute([], source["rates"]), "documents": [], "recipient": None, "phone": None}
    contracts, selected_payments, selected_reliefs = [], {}, {}
    for doc in docs:
        review = doc["review"]
        if not review or not review.get("evidence_clear"):
            raise ValueError("包含未完成原件核对的资料")
        contracts.append({"id": doc["id"], "fields": review["fields"], "stop": review.get("stop") or None})
        for p in source["payments"]:
            if str(p["序号"]) in review["payment_ids"]:
                selected_payments[str(p["序号"])] = p
        for r in source["reliefs"]:
            if r["批准文号"] in review.get("relief_ids", []):
                selected_reliefs[r["批准文号"]] = r
    result = compute(contracts, source["rates"], selected_payments.values(), selected_reliefs.values())
    last = max(docs, key=lambda d: (d["review"]["fields"]["start"], d["id"]))
    f = last["review"]["fields"]
    regs = match_registry(f, source["registry"])
    reg = regs[0] if regs else None
    recipient = reg["权利人"] if reg else f["tenant"]
    phone = reg.get("联系电话") if reg else None
    if not phone:
        relevant = sorted(
            [d for d in docs if d["review"]["fields"]["tenant"] == recipient],
            key=lambda d: d["review"]["fields"]["start"],
            reverse=True,
        )
        phone = next(
            (
                d["review"]["fields"].get("phone")
                for d in relevant
                if len(d["review"]["fields"].get("phone", "")) == 11
            ),
            "",
        )
    issues = result["issues"]
    if not reg:
        issues.append({"type": "登记数据中无对应宗地", "reason": "未找到唯一登记映射"})
    for doc in docs:
        r, fields = doc["review"], doc["review"]["fields"]
        if reg and clean(fields["address"]) != clean(reg["坐落"]):
            issues.append(
                {
                    "type": "坐落或宗地号与登记不一致",
                    "document": doc["id"],
                    "reason": "原文坐落写法不同，已保留原文",
                }
            )
        if reg and dec(fields["area"]) != dec(reg["宗地面积（㎡）"]):
            issues.append(
                {
                    "type": "面积与登记不一致",
                    "document": doc["id"],
                    "reason": f"合同{fields['area']}；登记{reg['宗地面积（㎡）']}",
                }
            )
        if fields.get("phone") and len(fields["phone"]) != 11:
            issues.append({"type": "联系电话缺失或无效", "document": doc["id"], "reason": "位数不符"})
        if r.get("uncertainty"):
            issues.append({"type": "存疑待确认", "document": doc["id"], "reason": r["uncertainty"]})
    return {
        **result,
        "rate_version": source.get("rate_version", "source-unversioned"),
        "documents": sorted(d["id"] for d in docs),
        "recipient": recipient,
        "phone": phone,
        "registry_unit": reg["不动产单元号"] if reg else "",
        "address": reg["坐落"] if reg else f["address"],
        "latest_document": last["id"],
        "source_manifest": dumps(
            {"payments": sorted(selected_payments), "reliefs": sorted(selected_reliefs)}
        ),
        "coverage": "核心计租/缴费/部分问题规则；尚未覆盖完整13类自动判定",
    }
