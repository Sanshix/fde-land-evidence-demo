"""一宗一档查询、到期/欠缴分级提醒、统计与业务导出（赛题表2 第2、4、8 项）。

只读已人工确认发布的台账快照；不改写任何金额。
"""

import csv
import io
from datetime import date, timedelta
from decimal import Decimal

from . import store
from .business import address_key
from .domain import AS_OF, FIELDS, dec, money, normalize_mode, tenant_kind

LEDGER_COLUMNS = [
    "宗地键", "档案号列表", "不动产单元号", "登记坐落", "登记面积", "合同面积", "用途", "土地级别", "计租方式",
    "合同状态", "合同终止日", "合同承租人", "登记权利人", "催缴对象", "联系电话", "2026年度应缴", "累计应缴",
    "累计实缴", "欠缴金额", "欠缴年度", "处理方式",
]
CONTRACT_COLUMNS = ["档案号", "页数", "合同类型", "合同编号", "原合同编号", "签订日期", "出租人", "承租人", "承租人类型",
                    "证件号码", "联系电话", "住所", "宗地号", "坐落", "土地面积", "用途", "土地级别", "租赁起始日",
                    "租赁终止日", "租赁年限", "计租方式", "年租金标准", "年租金小写", "年租金大写", "有无宗地图",
                    "涂改字段", "涂改前内容", "涂改处有无签章", "手写批注", "处理方式"]


def entries(db):
    out = []
    for row in db.execute("SELECT id FROM parcels WHERE merged_into IS NULL ORDER BY id"):
        latest = store.latest(db, row[0])
        if latest and latest["payload"].get("documents"):
            out.append(latest)
    return out


def street_of(address):
    text = address_key(address)
    for marker in ("大街", "路", "街", "巷", "道"):
        if marker in text:
            return text[: text.index(marker) + len(marker)]
    return text


def filtered(rows, street=None, status=None, arrears=None, keyword=None):
    out = []
    for pub in rows:
        p = pub["payload"]
        ledger = p.get("ledger", {})
        if street and street not in (p.get("address") or ""):
            continue
        if status and ledger.get("合同状态") != status:
            continue
        owing = dec(p["arrears"]) > 0
        if arrears == "欠缴" and not owing or arrears == "未欠缴" and owing:
            continue
        if keyword and keyword not in " ".join(
            str(x) for x in (p.get("recipient"), p.get("address"), p.get("registry_unit"), ledger.get("合同承租人"))
        ):
            continue
        out.append(pub)
    return out


def reminders(rows, today=AS_OF, horizon_days=90):
    """分级提醒：红=已到期未续或欠缴≥3个年度；橙=0–30天内到期或欠缴1-2个年度；黄=31–90天内到期的预告。"""
    items = []
    for pub in rows:
        p = pub["payload"]
        ledger = p.get("ledger", {})
        end = ledger.get("合同终止日")
        base = {"parcel_id": pub["parcel_id"], "publication": pub["id"], "recipient": p.get("recipient"),
                "address": p.get("address"), "phone": p.get("phone")}
        if end:
            end_day = date.fromisoformat(end)
            if end_day < today and ledger.get("合同状态") != "已解除/收回":
                items.append({**base, "kind": "合同到期", "level": "红", "date": end,
                              "message": f"合同已于{end}期满未续，仍按原方式计租"})
            elif ledger.get("合同状态") != "已解除/收回" and today <= end_day <= today + timedelta(days=horizon_days):
                level = "橙" if end_day <= today + timedelta(days=30) else "黄"
                items.append({**base, "kind": "合同到期", "level": level, "date": end,
                              "message": f"合同将于{end}期满，提醒办理续期（期满前三个月提出续租申请）"})
        owing = [r for r in p.get("annual", []) if dec(r["arrears"]) > 0]
        if owing and dec(p["arrears"]) > 0:
            first = min(r["year"] for r in owing)
            items.append({**base, "kind": "欠缴", "level": "红" if len(owing) >= 3 else "橙",
                          "date": f"{first}-06-30", "years": [r["year"] for r in owing],
                          "message": f"欠缴{len(owing)}个年度，共{p['arrears']}元"})
    order = {"红": 0, "橙": 1, "黄": 2}
    return sorted(items, key=lambda x: (order[x["level"]], x["date"]))


def _ics_text(value):
    return str(value or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics(items):
    """日历订阅文件：经办人员可导入 Outlook/手机日历（“使用方式接近日历提醒”）。"""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//land-lease//reminders//CN"]
    for i, item in enumerate(items):
        day = item["date"].replace("-", "")
        lines += ["BEGIN:VEVENT", f"UID:{item['parcel_id']}-{item['kind']}-{i}@land-lease", f"DTSTART;VALUE=DATE:{day}",
                  "SUMMARY:" + _ics_text(f"[{item['level']}]{item['kind']} {item['recipient']} {item['address']}"),
                  "DESCRIPTION:" + _ics_text(item["message"]), "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def statistics(rows, today=AS_OF):
    due = sum((dec(r["payload"]["due"]) for r in rows), Decimal(0))
    paid = sum((dec(r["payload"]["paid"]) for r in rows), Decimal(0))
    arrears = sum((max(Decimal(0), dec(r["payload"]["arrears"])) for r in rows), Decimal(0))
    by_status, by_street = {}, {}
    for pub in rows:
        p = pub["payload"]
        status = p.get("ledger", {}).get("合同状态", "")
        by_status[status] = by_status.get(status, 0) + 1
        s = by_street.setdefault(street_of(p.get("address")), {"宗地数": 0, "应缴": Decimal(0), "实缴": Decimal(0), "欠缴": Decimal(0)})
        s["宗地数"] += 1
        s["应缴"] += dec(p["due"])
        s["实缴"] += dec(p["paid"])
        s["欠缴"] += max(Decimal(0), dec(p["arrears"]))
    year_due = sum((dec(a["due"]) for r in rows for a in r["payload"]["annual"] if a["year"] == today.year), Decimal(0))
    year_paid = sum((dec(a["paid"]) for r in rows for a in r["payload"]["annual"] if a["year"] == today.year), Decimal(0))
    expiring = [r for r in rows if r["payload"].get("ledger", {}).get("合同终止日")
                and today <= date.fromisoformat(r["payload"]["ledger"]["合同终止日"]) <= today + timedelta(days=90)]
    return {
        "as_of": today.isoformat(),
        "宗地数": len(rows),
        "欠缴宗地数": sum(dec(r["payload"]["arrears"]) > 0 for r in rows),
        "累计应缴": str(money(due)),
        "累计实缴": str(money(paid)),
        "欠缴合计": str(money(arrears)),
        "累计收缴率": f"{(paid / due * 100).quantize(Decimal('0.1')) if due else 0}%",
        f"{today.year}年度应缴": str(money(year_due)),
        f"{today.year}年度实缴": str(money(year_paid)),
        f"{today.year}年度收缴率": f"{(year_paid / year_due * 100).quantize(Decimal('0.1')) if year_due else 0}%",
        "按合同状态": by_status,
        "已到期未续": by_status.get("到期未续", 0),
        "90天内到期": len(expiring),
        "按街路": {k: {kk: (str(money(vv)) if isinstance(vv, Decimal) else vv) for kk, vv in v.items()} for k, v in sorted(by_street.items())},
    }


def ledger_row(pub, names, method):
    p = pub["payload"]
    ledger = dict(p.get("ledger", {}))
    ledger.pop("土地级别依据", None)
    if ledger.get("计租方式") == "未勾选":
        ledger["计租方式"] = ""
    return {
        "宗地键": names.get(p.get("latest_document"), p.get("latest_document")),
        "档案号列表": ";".join(sorted(names.get(i, i) for i in p["documents"])),
        "不动产单元号": p.get("registry_unit", ""),
        **ledger,
        "处理方式": method,
    }


def review_method_label(method):
    """Keep recorded verification provenance, including non-human test reviews."""
    return "自动通过" if method == "批量确认" else method or "处理方式未记录"


def contract_row(doc):
    f = doc["review"]["fields"]
    row = {"档案号": doc["name"], "页数": len([p for p in doc["pages"] if p.get("path")]) or len(doc["pages"])}
    row.update({label: f.get(key, "") for key, label in FIELDS.items()})
    row["承租人类型"] = tenant_kind(f.get("identity"))
    if normalize_mode(f.get("mode")) == "未勾选":
        row["计租方式"] = ""
    if f.get("start") and f.get("end"):
        start, end = date.fromisoformat(f["start"]), date.fromisoformat(f["end"])
        row["租赁年限"] = round(((end - start).days + 1) / 365.25)
    row["处理方式"] = review_method_label(doc["review"].get("method"))
    return {k: row.get(k, "") for k in CONTRACT_COLUMNS}


def csv_bytes(rows, columns):
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8-sig")
