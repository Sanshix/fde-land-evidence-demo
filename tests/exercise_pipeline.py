"""Exercise-set pipeline used by tests and by `python3 -m tests.exercise_pipeline`.

It feeds *verified* contract fields (the 合同要素 the reviewer confirmed; here taken from the exercise
answer key to isolate the rule engine from OCR) through the same business code the API uses:
parcel grouping → payment candidates → calculate_documents → export rows. The output directory can
be scored with the organisers' score.py. Recognition accuracy is a separate measurement.
"""

import csv
import sys
from pathlib import Path

from app import business
from app.domain import FIELDS, tenant_kind

LABEL_TO_KEY = {label: key for key, label in FIELDS.items()}


def truth_documents(answer_dir):
    rows = list(csv.DictReader(open(Path(answer_dir) / "合同要素.csv", encoding="utf-8-sig")))
    docs = []
    for row in rows:
        fields = {key: (row.get(label) or "").strip() for label, key in LABEL_TO_KEY.items()}
        if fields["doc_type"] != "变更" and not fields["mode"]:
            fields["mode"] = "未勾选"  # reviewer states explicitly that no option is ticked
        fields["mode"] = {"随标准调整": "随标准调整"}.get(fields["mode"], fields["mode"])
        hint = business.completeness_hint(fields["doc_type"], int(row["页数"] or 0), fields["has_map"])
        if hint and not fields.get("missing_pages"):
            fields["missing_pages"] = hint  # 系统提示，复核人员看原件确认
        docs.append({"id": row["档案号"], "name": row["档案号"], "pages": int(row["页数"] or 0), "fields": fields})
    return docs


def group(docs, source):
    """Sequential confirmation as the API does: registry/cadastre key, then 原合同编号 links."""
    parent = {}

    def find(x):
        while parent.get(x, x) != x:
            x = parent[x]
        return x

    by_key = {}
    for d in docs:
        key = business.parcel_key(d["fields"], source)
        parent.setdefault(d["id"], d["id"])
        if key in by_key:
            parent[find(d["id"])] = find(by_key[key])
        else:
            by_key[key] = d["id"]
    numbers = {d["fields"]["contract_no"]: d["id"] for d in docs if d["fields"].get("contract_no")}
    for d in docs:
        original = d["fields"].get("original_contract_no")
        if original in numbers:
            parent[find(d["id"])] = find(numbers[original])
    groups = {}
    for d in docs:
        groups.setdefault(find(d["id"]), []).append(d)
    return list(groups.values())


def run(answer_dir, out_dir, source=None):
    source = source or business.sources()
    docs = truth_documents(answer_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ledgers, issues, errors = [], [], []
    for members in group(docs, source):
        contract_fields = [m["fields"] for m in members]
        wrapped = []
        for m in members:
            matches = business.suggest(m["fields"], source, contract_fields)
            wrapped.append(
                {
                    "id": m["id"],
                    "review": {
                        "fields": m["fields"],
                        "evidence_clear": True,
                        # Oracle fixture: assume a reviewer explicitly accepted ALL these candidates,
                        # including weak name-only matches. This is not batch-confirm eligibility.
                        "payment_ids": [str(p["序号"]) for p in matches["payments"]],
                        "relief_ids": [r["批准文号"] for r in matches["reliefs"]],
                    },
                }
            )
        try:
            calc = business.calculate_documents(wrapped, source)
        except ValueError as e:
            errors.append((",".join(m["id"] for m in members), str(e)))
            continue
        ledgers.append(
            {
                "宗地键": calc["latest_document"],
                "档案号列表": ";".join(calc["documents"]),
                "不动产单元号": calc["registry_unit"],
                **{k: v for k, v in calc["ledger"].items() if k != "土地级别依据"},
                "处理方式": "自动通过",
            }
        )
        issues += [{"档案号": i["document"], "问题类型": i["type"], "说明": i["reason"]} for i in calc["issues"]]
    contract_rows = []
    for d in docs:
        f = d["fields"]
        row = {"档案号": d["id"], "页数": d["pages"]}
        row.update({label: f.get(key, "") for key, label in FIELDS.items()})
        if row["计租方式"] == "未勾选":
            row["计租方式"] = ""
        row["承租人类型"] = tenant_kind(f.get("identity"))
        row["处理方式"] = "自动通过"
        contract_rows.append(row)
    for name, rows in (("合同要素.csv", contract_rows), ("宗地台账.csv", ledgers), ("问题清单.csv", issues)):
        with open(out_dir / name, "w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return {"ledgers": ledgers, "issues": issues, "errors": errors}


if __name__ == "__main__":
    result = run(Path(sys.argv[1]), Path(sys.argv[2]))
    print(len(result["ledgers"]), "parcels;", len(result["issues"]), "issues; errors:", result["errors"])
