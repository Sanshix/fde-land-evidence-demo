"""Offline field diagnostics for frozen recognition evidence, with no publication or aggregate score claims."""

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.domain import FIELDS as FIELD_LABELS  # noqa: E402
from app.evidence import comparison_value  # noqa: E402
from scripts.score_submission import FIELDS, KEY, ratio  # noqa: E402

LABEL_IDS = {label: key for key, label in FIELD_LABELS.items()}


def read_json(path, default):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default


def safe_compare(field, value):
    try:
        return FIELDS[field](value)
    except (ValueError, TypeError, OverflowError):
        return "INVALID:" + str(value)


def unique_candidate(key, rows):
    """Selection is independent of the answer; ambiguity remains withheld."""
    observed = [row for row in rows if isinstance(row, dict) and row.get("kind") != "context_suggestion"
                and row.get("model") and row.get("source_page_id") and str(row.get("raw_text") or "").strip()
                and isinstance(row.get("value"), str) and row["value"].strip()]
    values = {comparison_value(key, row["value"]) for row in observed}
    roles = {row.get("text_role") for row in observed}
    if "crossed_out" in roles or "alternative" in roles:
        return None, "withheld_alteration", sorted(values)
    if len(values) > 1:
        return None, "withheld_conflict", sorted(values)
    if not values:
        return None, "missing", []
    return values.pop(), "unique_observed", []


def evaluate(run_path, answers_path):
    run = Path(run_path).resolve()
    answers_path = Path(answers_path).resolve()
    answers_file = answers_path / "合同要素.csv"
    with answers_file.open(encoding="utf-8-sig", newline="") as stream:
        answers = list(csv.DictReader(stream))
    if not answers or any(any(field not in row for field in ["档案号", *FIELDS]) for row in answers):
        raise ValueError("合同标准答案缺少字段或没有数据")
    gold = {row["档案号"]: row for row in answers}
    documents_path = run / "documents.json"
    documents = read_json(documents_path, None)
    if not isinstance(documents, list) or any(not isinstance(d, dict) or not isinstance(d.get("name"), str) for d in documents):
        raise ValueError("运行目录需要 documents.json 档案清单")
    names = [document["name"] for document in documents]
    if any(name in {"", ".", ".."} or "/" in name or "\\" in name for name in names):
        raise ValueError("运行清单档案号包含不规范路径")
    if len(names) != len(set(names)):
        raise ValueError("运行清单含重复档案号，不能重复计算覆盖率")
    selected = [name for name in names if name in gold]
    fields = {field: {"correct": 0, "selected_truth_nonempty": 0, "all_truth_nonempty": 0,
                      "covered_truth_nonempty": 0, "unique_produced": 0, "produced_correct": 0,
                      "unexpected_nonempty": 0, "withheld_conflicts": 0} for field in FIELDS}
    for actual in answers:
        for field in FIELDS:
            fields[field]["all_truth_nonempty"] += safe_compare(field, actual[field]) != ""
    errors, details, field_rows, hashes = [], [], [], {}
    hashes[str(documents_path.relative_to(run))] = hashlib.sha256(documents_path.read_bytes()).hexdigest()
    for document in documents:
        name = document["name"]
        folder = run / "recognition" / name
        result_path, outcome_path = folder / "result.json", folder / "outcome.json"
        result = read_json(result_path, {})
        outcome = read_json(outcome_path, {})
        if not isinstance(result, dict) or not isinstance(outcome, dict):
            raise ValueError("识别结果或终态必须为JSON对象")
        for path in (result_path, outcome_path):
            if path.is_file():
                hashes[str(path.relative_to(run))] = hashlib.sha256(path.read_bytes()).hexdigest()
        actual = gold.get(name)
        candidates = result.get("candidates") or {}
        unique = {}
        statuses = {}
        field_correct = denominator = produced = produced_correct = covered = conflicts = 0
        for field in FIELDS:
            key = LABEL_IDS[field]
            value, status, alternatives = unique_candidate(key, candidates.get(key, []))
            statuses[field] = status
            unique[field] = value
            if actual is None:
                continue
            expected = safe_compare(field, actual[field])
            comparison = safe_compare(field, value) if value is not None else None
            correct = value is not None and comparison == expected
            metric = fields[field]
            nonempty = expected != ""
            metric["selected_truth_nonempty"] += nonempty
            metric["correct"] += nonempty and correct
            metric["covered_truth_nonempty"] += nonempty and value is not None
            metric["unique_produced"] += value is not None
            metric["produced_correct"] += correct
            metric["unexpected_nonempty"] += expected == "" and value is not None
            metric["withheld_conflicts"] += status.startswith("withheld")
            denominator += nonempty
            field_correct += nonempty and correct
            covered += nonempty and value is not None
            produced += value is not None
            produced_correct += correct
            conflicts += status.startswith("withheld")
            field_rows.append({"档案号": name, "字段": field, "候选状态": status, "唯一候选": value or "",
                               "标准答案": actual[field], "相同": correct, "计入非空分母": nonempty,
                               "冲突候选": json.dumps(alternatives, ensure_ascii=False) if alternatives else ""})
            if not correct and (nonempty or value is not None):
                errors.append(field_rows[-1])
        key_all = actual is not None and all(
            unique[field] is not None and safe_compare(field, unique[field]) == safe_compare(field, actual[field])
            or unique[field] is None and safe_compare(field, actual[field]) == "" and statuses[field] == "missing"
            for field in KEY)
        all_correct = actual is not None and all(
            unique[field] is not None and safe_compare(field, unique[field]) == safe_compare(field, actual[field])
            or unique[field] is None and safe_compare(field, actual[field]) == "" and statuses[field] == "missing"
            for field in FIELDS)
        # No result is not an all-correct document, including a hypothetical blank gold row.
        key_all = key_all and bool(result)
        all_correct = all_correct and bool(result)
        coverage = result.get("page_coverage") or {}
        completed = coverage.get("completed", outcome.get("completed_pages", []))
        completed_count = len(completed) if isinstance(completed, list) else completed if type(completed) is int else 0
        page_count = coverage.get("total", outcome.get("pages", len(document.get("files", []))))
        details.append({"name": name, "matched_answer": actual is not None, "has_result": bool(result),
                        "state": outcome.get("state", "not_recorded"),
                        "completion_status": result.get("completion_status", outcome.get("completion_status", outcome.get("status", "not_run"))),
                        "published_recorded": outcome.get("published") if type(outcome.get("published")) is bool else None,
                        "pages": page_count, "protocol_completed_pages": completed_count,
                        "unknown_attempts": len(result.get("unknown_attempts", outcome.get("unknown_attempts", [])) or []),
                        "correct_nonempty": field_correct, "truth_nonempty": denominator,
                        "unique_produced": produced, "produced_correct": produced_correct,
                        "covered_nonempty": covered, "withheld_conflicts": conflicts,
                        "key_all_correct": key_all, "all_15_correct": all_correct})
    correct = sum(m["correct"] for m in fields.values())
    selected_denominator = sum(m["selected_truth_nonempty"] for m in fields.values())
    all_denominator = sum(m["all_truth_nonempty"] for m in fields.values())
    covered = sum(m["covered_truth_nonempty"] for m in fields.values())
    produced = sum(m["unique_produced"] for m in fields.values())
    produced_correct = sum(m["produced_correct"] for m in fields.values())
    return {
        "kind": "offline_recognition_field_diagnostic", "run": str(run), "answers": str(answers_path),
        "definition": "仅对冻结识别结果选取无冲突的原文候选；不采纳上下文推断、不择优选择涂改后值、不使用答案选择候选。",
        "documents": {"selected": len(documents), "selected_with_answers": len(selected), "all_gold": len(answers),
                      "not_selected_gold": len(gold.keys() - set(names)), "extra_selected": sorted(set(names) - gold.keys()),
                      "with_result": sum(d["has_result"] for d in details)},
        "field_accuracy": {"selected_fixed_denominator": ratio(correct, selected_denominator),
                           "all_gold_fixed_denominator": ratio(correct, all_denominator),
                           "all_gold_note": "未选入本轮的档案也留在全量分母；这是覆盖诊断，不代表已对全部档案完成测试。",
                           "precision_unique_produced": ratio(produced_correct, produced),
                           "coverage_selected": ratio(covered, selected_denominator),
                           "coverage_all_gold": ratio(covered, all_denominator),
                           "unexpected_nonempty": sum(m["unexpected_nonempty"] for m in fields.values()),
                           "per_field": fields},
        "whole_contract": {"key_all_correct": sum(d["key_all_correct"] for d in details),
                           "all_15_correct": sum(d["all_15_correct"] for d in details), "selected_with_answers": len(selected)},
        "protocol_coverage": {"completed_pages": sum(d["protocol_completed_pages"] for d in details),
                              "selected_pages": sum(d["pages"] for d in details),
                              "definition": "读取协议完成不等于字段正确、证据页正确或人工核验完成"},
        "completion_states": dict(Counter(d["completion_status"] for d in details)),
        "publication": {"recorded_true": sum(d["published_recorded"] is True for d in details),
                        "recorded_false": sum(d["published_recorded"] is False for d in details),
                        "unknown": sum(d["published_recorded"] is None for d in details),
                        "note": "不生成人工复核/自动通过标签，不计算产品发布得分或核算得分"},
        "evidence_sha256": hashes, "answers_sha256": hashlib.sha256(answers_file.read_bytes()).hexdigest(),
        "details": details, "field_rows": field_rows, "errors": errors,
    }


def write_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "recognition_score.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for name, rows, default in [("识别文档状态.csv", report["details"], ["name"]),
                                ("识别字段差异.csv", report["errors"], ["档案号", "字段"]),
                                ("识别字段明细.csv", report["field_rows"], ["档案号", "字段"])]:
        with (output / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]) if rows else default)
            writer.writeheader()
            writer.writerows(rows)
    accuracy = report["field_accuracy"]
    selected = accuracy["selected_fixed_denominator"]
    all_gold = accuracy["all_gold_fixed_denominator"]
    precision = accuracy["precision_unique_produced"]
    coverage = accuracy["coverage_selected"]
    text = f"""# 冻结识别结果字段诊断

选入本轮 {report['documents']['selected']} 份，其中 {report['documents']['with_result']} 份有识别结果。

- 本轮固定非空分母：正确 {selected['correct']}/{selected['denominator']}。
- 全部标准答案固定非空分母：正确 {all_gold['correct']}/{all_gold['denominator']}；未选档案留在分母，不能称作已完成全量测试。
- 本轮唯一原文候选覆盖：{coverage['correct']}/{coverage['denominator']}。
- 唯一输出精度：{precision['correct']}/{precision['denominator']}。
- 本轮关键字段全对：{report['whole_contract']['key_all_correct']}/{report['whole_contract']['selected_with_answers']}。

候选冲突、涂改前后和上下文推断均不择优。缺少结果仍计入本轮分母。协议完成不是人工确认、发布或证据页准确率；本报告不生成产品分数或人为处理方式。
"""
    (output / "识别字段诊断.md").write_text(text, encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = evaluate(args.run, args.answers)
        write_report(report, args.output)
    except (ValueError, OSError, KeyError, TypeError, csv.Error) as error:
        parser.error(str(error))
    print(json.dumps({"output": str(args.output.resolve()), "documents": report["documents"],
                      "field_accuracy": {k: v for k, v in report["field_accuracy"].items() if k != "per_field"},
                      "whole_contract": report["whole_contract"], "completion_states": report["completion_states"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
