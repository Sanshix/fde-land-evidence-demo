"""OFFLINE scoring only: reads frozen outputs and answers, never imports a model client.

Shadow calculations use unconfirmed model proposals in memory. They never write the
product database, confirm a document, or represent actual staff review.
"""

# ruff: noqa: E402
import argparse
import csv
import hashlib
import itertools
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.business import SAMPLES, calculate_documents, parcel_key, sources, suggest
from app.domain import FIELDS
from app.governance import normalize, parse_ocr_text

parser = argparse.ArgumentParser()
parser.add_argument("--run", type=Path, required=True)
args = parser.parse_args()
run = args.run.resolve()
stamp = str(__import__("time").time_ns())
destination = run / ("evaluation-" + stamp)
destination.mkdir()


def read_csv(path):
    with path.open(encoding="utf-8-sig") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, columns, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def normalized(key, value):
    values = normalize(key, value or "")
    return values[0] if len(values) == 1 else (value or "")


def choose(rows, key, prefer_current=True):
    # Deterministic, frozen policy; no answer-based selection, no confidence majority vote.
    current = [c for c in rows if c.get("text_role") == "current"] if prefer_current else []
    values = {normalized(key, c["value"]) for c in current or rows if c.get("text_role") != "crossed_out"}
    return next(iter(values)) if len(values) == 1 else ""


def comparison(field, value):
    value = re.sub(r"\s+", "", str(value or "")).translate(
        str.maketrans("０１２３４５６７８９（）：，．", "0123456789():,.")
    )
    if field in {"土地面积", "年租金小写"}:
        try:
            return round(float(value.replace(",", "").rstrip("元").rstrip("平方米")), 2)
        except ValueError:
            return value
    if field in {"签订日期", "租赁起始日", "租赁终止日"}:
        match = re.match(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", value)
        if match:
            try:
                return date(*map(int, match.groups())).isoformat()
            except ValueError:
                return "INVALID_DATE:" + value
    if field == "证件号码":
        return value.upper()
    if field == "用途":
        return value.replace("用地", "")
    if field == "计租方式":
        return "固定" if "固定" in value else ("随标准调整" if value else "")
    return value


documents = json.loads((run / "documents.json").read_text())
answers = read_csv(SAMPLES / "08_标准答案/合同要素.csv")
ledger_answers = read_csv(SAMPLES / "08_标准答案/宗地台账.csv")
issue_answers = read_csv(SAMPLES / "08_标准答案/问题清单.csv")
columns = list(answers[0]) + ["处理方式"]
ledger_columns = list(ledger_answers[0]) + ["处理方式"]
primary_rows, cascade_rows, cleaned_rows, outcomes, input_hashes = [], [], [], [], {}
results = {}
for document in documents:
    folder = run / "recognition" / document["name"]
    outcome = (
        json.loads((folder / "outcome.json").read_text())
        if (folder / "outcome.json").exists()
        else {"status": "not_completed", "name": document["name"], "pages": len(document["files"])}
    )
    outcomes.append(outcome)
    result = json.loads((folder / "result.json").read_text()) if (folder / "result.json").exists() else {}
    results[document["name"]] = result
    corrected_candidates = {
        key: list(value) for key, value in result.get("primary", {}).get("candidates", {}).items()
    }
    for page in result.get("pages", []):
        for stage in page.get("stages", []):
            raw_text = stage.get("raw_response", {}).get("text")
            if stage.get("status") == "completed" and isinstance(raw_text, str):
                parsed, _ = parse_ocr_text(
                    raw_text,
                    {"id": page["id"]},
                    {"model_id": stage["requested_model"], "provider": stage["provider"]},
                )
                for key, values in parsed.items():
                    corrected_candidates.setdefault(key, []).extend(values)
    for file in folder.glob("*.json"):
        input_hashes[str(file.relative_to(run))] = hashlib.sha256(file.read_bytes()).hexdigest()
    for rows, candidates in [
        (primary_rows, result.get("primary", {}).get("candidates", {})),
        (cascade_rows, result.get("candidates", {})),
        (cleaned_rows, corrected_candidates),
    ]:
        row = {"档案号": document["name"], "页数": len(document["files"]), "处理方式": "人工复核"}
        row.update(
            {
                label: normalized(key, choose(candidates.get(key, []), key, rows is primary_rows))
                for key, label in FIELDS.items()
            }
        )
        rows.append(row)

source = sources()
scored_fields = [
    "合同编号",
    "合同类型",
    "签订日期",
    "承租人",
    "证件号码",
    "联系电话",
    "宗地号",
    "坐落",
    "土地面积",
    "用途",
    "租赁起始日",
    "租赁终止日",
    "计租方式",
    "年租金小写",
    "年租金大写",
]
critical = [f for f in scored_fields if f not in {"合同类型", "联系电话", "用途"}]
development = {
    "LX-001",
    "LX-004",
    "LX-014",
    "LX-045",
    "LX-021",
    "LX-017",
    "LX-034",
    "LX-025",
    "LX-018",
    "LX-008",
}


def diagnose(rows, names=None):
    predicted = {r["档案号"]: r for r in rows}
    truth = [a for a in answers if names is None or a["档案号"] in names]
    fields, errors = {}, []
    for field in scored_fields:
        ok = denominator = empty_truth = 0
        for actual in truth:
            value = predicted[actual["档案号"]].get(field, "")
            expected = actual[field]
            x, y = comparison(field, expected), comparison(field, value)
            if x == "" and y == "":
                empty_truth += 1
                continue
            denominator += 1
            ok += x == y
            if x != y:
                errors.append(
                    {"档案号": actual["档案号"], "字段": field, "原始预测": value, "标准答案": expected}
                )
        fields[field] = {
            "correct": ok,
            "denominator": denominator,
            "both_empty_excluded": empty_truth,
            "accuracy": ok / denominator if denominator else None,
        }

    def all_correct(a, fs):
        return all(comparison(f, a[f]) == comparison(f, predicted[a["档案号"]].get(f)) for f in fs)

    return {
        "documents": len(truth),
        "key_all_correct": sum(all_correct(a, critical) for a in truth),
        "all_15_correct": sum(all_correct(a, scored_fields) for a in truth),
        "fields": fields,
        "errors": errors,
    }


def shadow(rows):
    groups, isolated = {}, []
    for row in rows:
        fields = {k: row.get(label, "") for k, label in FIELDS.items()}
        if not fields["parcel_no"]:
            isolated.append({"name": row["档案号"], "reason": "no_unique_parcel_candidate"})
            continue
        try:
            key = parcel_key(fields, source)
        except ValueError as error:
            isolated.append({"name": row["档案号"], "reason": str(error)})
            continue
        matching = suggest(fields, source)
        groups.setdefault(key, []).append(
            {
                "id": row["档案号"],
                "review": {
                    "fields": fields,
                    "evidence_clear": True,
                    "payment_ids": [str(p["序号"]) for p in matching["payments"]],
                    "relief_ids": [r["批准文号"] for r in matching["reliefs"]],
                },
            }
        )
    ledgers, issues, failures = [], [], []
    for key, docs in groups.items():
        latest = max(docs, key=lambda d: (d["review"]["fields"].get("start", ""), d["id"]))
        parcel = latest["id"]
        row = {
            "宗地键": parcel,
            "档案号列表": ";".join(sorted(d["id"] for d in docs)),
            "不动产单元号": key if not key.startswith("unregistered:") else "",
            "处理方式": "人工复核",
        }
        try:
            result = calculate_documents(docs, source)
            row.update(
                {
                    "累计应缴": result["due"],
                    "累计实缴": result["paid"],
                    "欠缴金额": result["arrears"],
                    "2026年度应缴": next((x["due"] for x in result["annual"] if x["year"] == 2026), "0.00"),
                    "催缴对象": result["recipient"],
                    "联系电话": result["phone"],
                }
            )
            for issue in result["issues"]:
                issues.append(
                    {
                        "档案号": issue.get("document", parcel),
                        "问题类型": issue["type"],
                        "说明": issue["reason"],
                    }
                )
        except Exception as error:
            failures.append(
                {
                    "parcel": parcel,
                    "documents": row["档案号列表"],
                    "reason": str(error),
                    "error_type": type(error).__name__,
                }
            )
        ledgers.append(row)
    return (
        ledgers,
        issues,
        {
            "groups": len(groups),
            "calculation_completed": len(groups) - len(failures),
            "failed_groups": failures,
            "isolated_documents": isolated,
            "label": "unconfirmed model proposals; in-memory simulation only, never official ledger or actual human review",
        },
    )


metrics = {
    "run": run.name,
    "eval_id": destination.name,
    "frozen_recognition_sha256": input_hashes,
    "coverage": {
        "expected_documents": 50,
        "expected_pages": 139,
        "outcomes": dict(Counter(o["status"] for o in outcomes)),
        "successful_primary_pages": sum(
            len(r.get("primary", {}).get("input_pages", [])) for r in results.values()
        ),
    },
    "platforms": {
        "siliconflow": "real success; both models; coverage below",
        "aliyun": "HTTP401 invalid_api_key, no model result",
        "baidu": "HTTP401 invalid_iam_token, no model result",
    },
    "human_seconds": None,
    "actual_staff_reviewed": 0,
    "automatic_effective_publications": 0,
}
for label, rows in [
    ("primary", primary_rows),
    ("cascade_conservative", cascade_rows),
    ("cascade_parser_v2_replay", cleaned_rows),
]:
    path = destination / label
    path.mkdir()
    ledger, problems, status = shadow(rows)
    write_csv(path / "合同要素.csv", columns, rows)
    write_csv(path / "宗地台账.csv", ledger_columns, ledger)
    write_csv(path / "问题清单.csv", list(issue_answers[0]), problems)
    diagnostic = diagnose(rows)
    write_csv(path / "field-errors.csv", ["档案号", "字段", "原始预测", "标准答案"], diagnostic.pop("errors"))
    diagnostic["development_10"] = {k: v for k, v in diagnose(rows, development).items() if k != "errors"}
    diagnostic["held_out_40"] = {
        k: v
        for k, v in diagnose(rows, set(r["档案号"] for r in answers) - development).items()
        if k != "errors"
    }
    actual_pairs = {
        frozenset(p) for a in ledger_answers for p in itertools.combinations(a["档案号列表"].split(";"), 2)
    }
    predicted_pairs = {
        frozenset(p) for a in ledger for p in itertools.combinations(a["档案号列表"].split(";"), 2)
    }
    diagnostic["grouping"] = {
        "correct_pairs": len(actual_pairs & predicted_pairs),
        "false_merged_pairs": len(predicted_pairs - actual_pairs),
        "missed_pairs": len(actual_pairs - predicted_pairs),
        "actual_pairs": len(actual_pairs),
        "predicted_pairs": len(predicted_pairs),
    }
    diagnostic["shadow"] = status
    script = subprocess.run(
        [sys.executable, str(SAMPLES / "score.py"), str(SAMPLES / "08_标准答案"), str(path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    (path / "official-scorer.txt").write_text(script.stdout + script.stderr)
    diagnostic["official_scorer_exit_code"] = script.returncode
    metrics[label] = diagnostic

usage, successes, failures, timings = Counter(), Counter(), Counter(), []
for result in results.values():
    responses = [result.get("primary", {})] + [
        stage for page in result.get("pages", []) for stage in page.get("stages", [])
    ]
    for response in responses:
        if response.get("returned_model"):
            successes[response["returned_model"]] += 1
            usage.update(
                {k: v for k, v in (response.get("usage") or {}).items() if isinstance(v, (int, float))}
            )
        if response.get("status") == "failed":
            failures[response.get("error_type", "unknown")] += 1
    if result.get("elapsed_seconds"):
        timings.append(result["elapsed_seconds"])
metrics["live_calls"] = {
    "completed_responses_by_model": dict(successes),
    "secondary_failures": dict(failures),
    "tokens": dict(usage),
    "monetary_cost": None,
    "cost_note": "unknown unit prices/billed failed calls; no fabricated zero",
    "document_elapsed_seconds": sorted(timings),
    "outcomes": outcomes,
}
(destination / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
(run / "latest-evaluation.txt").write_text(str(destination))
print(
    json.dumps(
        {
            "evaluation": str(destination),
            "coverage": metrics["coverage"],
            "primary_key_all_correct": metrics["primary"]["key_all_correct"],
            "cascade_key_all_correct": metrics["cascade_conservative"]["key_all_correct"],
        },
        ensure_ascii=False,
    )
)
