"""Draft outputs from confirmed immutable snapshots, never send messages."""

import io
import zipfile
from copy import deepcopy
from datetime import date
from decimal import Decimal
from xml.etree import ElementTree as ET

import openpyxl

from .business import SAMPLES
from .domain import dec, money

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
ET.register_namespace("w", W)


def replace_paragraph(p, values):
    texts = list(p.iter("{" + W + "}t"))
    original = "".join(t.text or "" for t in texts)
    updated = original
    for key, value in values.items():
        updated = updated.replace("【" + key + "】", str(value))
    if texts and updated != original:
        texts[0].text = updated
        for t in texts[1:]:
            t.text = ""


def notice_docx(notice, publication):
    p = publication
    fields = notice["contract_fields"]
    values = {
        "年份": "2026",
        "编号": "演示草稿-" + notice["id"],
        "催缴对象": p["recipient"],
        "坐落": p["address"],
        "不动产单元号": p["registry_unit"] or "待核实",
        "面积": fields["area"],
        "合同编号": fields.get("contract_no", "待核实"),
        "核算基准日": p["as_of"],
        "欠缴年度": "、".join(str(r["year"]) for r in p["annual"] if dec(r["arrears"]) > 0),
        "欠缴金额": p["arrears"],
        "欠缴金额大写": "待人工核验填写",
        "期限": "待主管部门填写",
        "缴费地点": "待主管部门配置",
        "核对地点": "待主管部门配置",
        "经办电话": "待主管部门配置",
        "发文日期": "草稿未发文",
        "到期日": fields["end"],
    }
    path = SAMPLES / "07_模板/国有土地租金催缴通知书（模板）.docx"
    output = io.BytesIO()
    with zipfile.ZipFile(path) as src, zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as dest:
        for entry in src.infolist():
            raw = src.read(entry.filename)
            if entry.filename == "word/document.xml":
                root = ET.fromstring(raw)
                for ptag in root.iter("{" + W + "}p"):
                    replace_paragraph(ptag, values)
                # Replace the single template-year row with actual annual rows.
                for table in root.iter("{" + W + "}tbl"):
                    for row in list(table):
                        text = "".join(t.text or "" for t in row.iter("{" + W + "}t"))
                        if "【年度】" in text:
                            index = list(table).index(row)
                            table.remove(row)
                            for offset, item in enumerate(p["annual"]):
                                copy = deepcopy(row)
                                for paragraph in copy.iter("{" + W + "}p"):
                                    replace_paragraph(
                                        paragraph,
                                        {
                                            "年度": item["year"],
                                            "应缴": item["due"],
                                            "已缴": item["paid"],
                                            "欠缴": item["arrears"],
                                        },
                                    )
                                table.insert(index + offset, copy)
                # Explicit draft watermark as a first paragraph, no automated legal issuance.
                body = root.find("{" + W + "}body")
                label = ET.Element("{" + W + "}p")
                run = ET.SubElement(label, "{" + W + "}r")
                text = ET.SubElement(run, "{" + W + "}t")
                text.text = "演示草稿 · 未发送 · 未完成发文审核；缴费指引、金额大写和模板可选段须人工核验"
                body.insert(0, label)
                raw = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            dest.writestr(entry, raw)
    return output.getvalue()


def tax_workbook(ledgers, docs, source, year, quarter):
    if not 1998 <= year <= 2026 or quarter not in (1, 2, 3, 4):
        raise ValueError("推送期间无效")
    start = date(year, (quarter - 1) * 3 + 1, 1)
    end = date(year, quarter * 3 + 1, 1) if quarter < 4 else date(year + 1, 1, 1)
    if start > date(2026, 9, 30):
        raise ValueError("推送期间晚于核算基准日")
    wb = openpyxl.load_workbook(SAMPLES / "07_模板/税务推送单（字段与样式）.xlsx")
    ws = wb["推送单"]
    ws.delete_rows(2, ws.max_row)
    by_id = {d["id"]: d for d in docs}
    for pub in ledgers:
        p = pub["payload"]
        if not p["documents"]:
            continue
        document = by_id[p["latest_document"]]
        fields = document["review"]["fields"]
        chosen = set()
        for identifier in p["documents"]:
            chosen.update(by_id[identifier]["review"]["payment_ids"])
        paid = {}
        for r in source["payments"]:
            day = date.fromisoformat(str(r["缴费日期"])[:10])
            if str(r["序号"]) in chosen and start <= day < end and day <= date(2026, 9, 30):
                paid[str(r["票据号"])] = r
        current = next((r for r in source["registry"] if r["不动产单元号"] == p["registry_unit"]), None)
        annual = next((r["due"] for r in p["annual"] if r["year"] == year), "0.00")
        # Quarter liability is not unambiguously defined by the template; never invent annual/4.
        ws.append(
            [
                ws.max_row,
                p["recipient"],
                str(current["证件号码"]) if current else fields.get("identity", ""),
                p["registry_unit"],
                p["address"],
                fields["area"],
                fields["use"],
                fields["start"],
                fields["end"],
                annual,
                None,
                str(money(sum((dec(r["金额（元）"]) for r in paid.values()), Decimal(0)))),
                "；".join(sorted({str(r["缴费日期"])[:10] for r in paid.values()})),
                "；".join(sorted(paid)),
                f"{year}年第{quarter}季度",
                f"模拟草稿未推送；本期应缴口径待核定，不能直接年租金除4；台账版本{pub['id']}",
            ]
        )
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            if isinstance(cell.value, str) and cell.value.startswith(("=", "+", "-", "@")):
                cell.value = "'" + cell.value
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()
