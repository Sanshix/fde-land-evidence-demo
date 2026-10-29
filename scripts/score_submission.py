"""Offline diagnostics for actual exports. No model clients, database writes, or answer-based repairs."""

import argparse
import csv
import hashlib
import io
import itertools
import json
import math
import re
import subprocess
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path, PurePosixPath

FILES = ("合同要素.csv", "宗地台账.csv", "问题清单.csv")
MAX_CSV_BYTES = 25 * 1024 * 1024
FW = str.maketrans("０１２３４５６７８９（）：，．", "0123456789():,.")
DEFAULT_SCORER = Path(__file__).resolve().parents[2] / "练习集" / "score.py"


def norm(value):
    return re.sub(r"\s+", "", str(value or "")).translate(FW)


def ndate(value):
    value = norm(value)
    match = re.match(r"(\d{4})\D(\d{1,2})\D(\d{1,2})", value)
    return date(*map(int, match.groups())).isoformat() if match else value


def nnum(value):
    try:
        return round(float(norm(value).replace(",", "").rstrip("元").rstrip("平方米")), 2)
    except ValueError:
        return norm(value)


FIELDS = {
    "合同编号": norm, "合同类型": norm, "签订日期": ndate, "承租人": norm,
    "证件号码": lambda v: norm(v).upper(), "联系电话": norm, "宗地号": norm,
    "坐落": norm, "土地面积": nnum, "用途": lambda v: norm(v).replace("用地", ""),
    "租赁起始日": ndate, "租赁终止日": ndate,
    "计租方式": lambda v: "固定" if "固定" in norm(v) else ("随标准调整" if norm(v) else ""),
    "年租金小写": nnum, "年租金大写": norm,
}
KEY = [f for f in FIELDS if f not in {"合同类型", "联系电话", "用途"}]


def review(row):
    return "复核" in norm((row or {}).get("处理方式"))


def ratio(correct, denominator):
    return {"correct": correct, "denominator": denominator,
            "accuracy": correct / denominator if denominator else None}


def close(actual, expected):
    try:
        return abs(float(norm(actual)) - float(norm(expected))) <= 0.011
    except ValueError:
        return False


def members(row):
    return [x for x in re.split(r"[;；,，、\s]+", norm(row.get("档案号列表"))) if x]


def groups(rows):
    return {name: row for row in rows for name in members(row)}


def set_f1(actual, predicted):
    hit = len(actual & predicted)
    precision = hit / len(predicted) if predicted else (1.0 if not actual else 0)
    recall = hit / len(actual) if actual else 1.0
    return {"f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
            "precision": precision, "recall": recall,
            "true_positive": hit, "expected": len(actual), "predicted": len(predicted)}


def load_submission(path, *, required=False):
    """Read the three root CSVs only. ZIP entries are never extracted or executed."""
    path = Path(path).resolve()
    payloads = {}
    if path.is_dir():
        for name in FILES:
            file = path / name
            if file.is_file():
                if file.stat().st_size > MAX_CSV_BYTES:
                    raise ValueError(f"CSV超过25MiB限制：{name}")
                payloads[name] = file.read_bytes()
    elif path.is_file() and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            seen = set()
            for entry in archive.infolist():
                key = PurePosixPath(entry.filename)
                if key.is_absolute() or ".." in key.parts or "\\" in entry.filename or "\x00" in entry.filename:
                    raise ValueError("ZIP包含越界或不规范路径")
                if entry.filename in FILES:
                    if entry.filename in seen:
                        raise ValueError("ZIP包含重名CSV")
                    seen.add(entry.filename)
                    if entry.file_size > MAX_CSV_BYTES:
                        raise ValueError(f"CSV超过25MiB限制：{entry.filename}")
                    payloads[entry.filename] = archive.read(entry)
    else:
        raise ValueError(f"提交必须是目录或ZIP：{path}")
    if required and any(name not in payloads for name in FILES):
        raise ValueError("标准答案目录缺少三个必需CSV")
    rows = {}
    for name in FILES:
        reader = csv.DictReader(io.StringIO(payloads.get(name, b"").decode("utf-8-sig")))
        rows[name] = list(reader)
        if payloads.get(name) and not reader.fieldnames:
            raise ValueError(f"CSV缺少列名：{name}")
    return rows, payloads


def _duplicates(rows, key):
    return sorted(k for k, count in Counter(norm(r.get(key)) for r in rows).items() if count > 1)


def score_rows(answers, submitted, *, source_kind="submission"):
    """Match official score.py semantics; diagnostics use a separately fixed nonempty-gold denominator."""
    gold_contracts = answers[FILES[0]]
    gold_ledgers = answers[FILES[1]]
    if not gold_contracts or not gold_ledgers:
        raise ValueError("标准答案的合同与宗地台账均不得为空")
    for row in gold_contracts:
        if "档案号" not in row or any(field not in row for field in FIELDS):
            raise ValueError("标准答案合同要素列不完整")
    for row in gold_ledgers:
        if any(field not in row for field in ("宗地键", "档案号列表", "不动产单元号", "欠缴金额", "累计应缴", "2026年度应缴", "催缴对象")):
            raise ValueError("标准答案宗地台账列不完整")
    actual = {r["档案号"]: r for r in gold_contracts}
    predicted = {norm(r.get("档案号")): r for r in submitted[FILES[0]]}
    gold_count = len(actual)
    per_field = {f: {"gold_nonempty": 0, "correct": 0, "submitted_gold_nonempty": 0,
                     "official_correct": 0, "official_denominator": 0, "unexpected_nonempty": 0} for f in FIELDS}
    errors, contracts = [], []
    points = 0
    auto_submitted = auto_wrong = key_correct = all_correct = submitted_nonempty = correct = 0
    for name, gold in actual.items():
        row = predicted.get(name)
        field_correct = truth_nonempty = 0
        equal = {}
        for field, transform in FIELDS.items():
            expected = transform(gold[field])
            value = transform(row.get(field, "")) if row is not None else None
            equal[field] = row is not None and value == expected
            metric = per_field[field]
            if expected != "":
                metric["gold_nonempty"] += 1
                truth_nonempty += 1
                metric["submitted_gold_nonempty"] += row is not None
                submitted_nonempty += row is not None
                metric["correct"] += equal[field]
                field_correct += equal[field]
                correct += equal[field]
            elif value not in {None, ""}:
                metric["unexpected_nonempty"] += 1
            # Official diagnostics include a blank gold field if a nonblank value is submitted.
            if not (expected == "" and value in {None, ""}):
                metric["official_denominator"] += 1
                metric["official_correct"] += value == expected
            if not equal[field] and not (expected == "" and value in {None, ""}):
                errors.append({"档案号": name, "字段": field, "提交值": row.get(field, "") if row else "",
                               "标准答案": gold[field], "原因": "缺少提交行" if row is None else "字段不同"})
        keys_ok = row is not None and all(equal[f] for f in KEY)
        all_ok = row is not None and all(equal.values())
        key_correct += keys_ok
        all_correct += all_ok
        manual = review(row)
        points += .5 if manual else (1 if keys_ok else -1)
        auto_submitted += row is not None and not manual
        auto_wrong += row is not None and not manual and not keys_ok
        contracts.append({"档案号": name, "已提交": row is not None,
                          "处理方式": "缺行（官方按自动错误扣分）" if row is None else "人工复核" if manual else "自动通过",
                          "关键字段全对": keys_ok, "全部15字段全对": all_ok,
                          "非空应评字段": truth_nonempty, "正确字段": field_correct,
                          "合同原始分": .5 if manual else 1 if keys_ok else -1})
    contract_score = 30 * max(0, points) / gold_count
    selected = submitted[FILES[1]]
    indexed = groups(selected)
    pairs_gold = {frozenset(pair) for row in gold_ledgers
                  for pair in itertools.combinations(sorted(re.split(r";", row["档案号列表"])), 2)}
    pairs_pred = {frozenset(pair) for row in selected for pair in itertools.combinations(sorted(members(row)), 2)}
    pairing = set_f1(pairs_gold, pairs_pred)
    singles = {r["档案号列表"] for r in gold_ledgers if ";" not in r["档案号列表"]}
    single_count = sum(name in indexed and len(members(indexed[name])) == 1 for name in singles)
    single_ok = single_count / max(1, len(singles))
    unit_correct = sum(bool(indexed.get(r["宗地键"])) and norm(indexed[r["宗地键"]].get("不动产单元号")) == norm(r["不动产单元号"])
                       for r in gold_ledgers)
    ledger_count = len(gold_ledgers)
    grouping_score = 15 * (.5 * (pairing["f1"] * .7 + single_ok * .3) + .5 * unit_correct / ledger_count)
    parcel_rows = []
    arrears_points = total_correct = annual_correct = recipient_correct = arrears_correct = 0
    ledger_auto = ledger_auto_wrong = 0
    for gold in gold_ledgers:
        row = indexed.get(gold["宗地键"])
        selected_row = row or {}
        amounts = {field: close(selected_row.get(field), gold[field]) for field in ("欠缴金额", "累计应缴", "2026年度应缴")}
        manual = review(row)
        arrears_points += .5 if manual else 1 if amounts["欠缴金额"] else -1
        total_correct += amounts["累计应缴"]
        annual_correct += amounts["2026年度应缴"]
        arrears_correct += amounts["欠缴金额"]
        who = norm(selected_row.get("催缴对象")) == norm(gold["催缴对象"])
        recipient_correct += who
        ledger_auto += row is not None and not manual
        ledger_auto_wrong += row is not None and not manual and not amounts["欠缴金额"]
        parcel_rows.append({"宗地键": gold["宗地键"], "标准档案号列表": gold["档案号列表"],
                            "已提交": row is not None,
                            "提交档案号列表": selected_row.get("档案号列表", ""),
                            "已知材料组完整": bool(row) and set(members(row)) == set(members(gold)),
                            "处理方式": "缺行" if row is None else "人工复核" if manual else "自动通过",
                            "欠缴正确": amounts["欠缴金额"], "累计应缴正确": amounts["累计应缴"],
                            "2026年度正确": amounts["2026年度应缴"], "催缴对象正确": who})
    calculation_score = 30 * (.5 * max(0, arrears_points) + .25 * total_correct + .25 * annual_correct) / ledger_count
    recipient_score = 5 * recipient_correct / ledger_count
    issue_gold = {(r["档案号"], r["问题类型"]) for r in answers[FILES[2]]}
    issue_pred = {(norm(r.get("档案号")), norm(r.get("问题类型"))) for r in submitted[FILES[2]]}
    ordinary = set_f1({x for x in issue_gold if x[1] != "存疑待确认"}, {x for x in issue_pred if x[1] != "存疑待确认"})
    ambiguous = set_f1({x for x in issue_gold if x[1] == "存疑待确认"}, {x for x in issue_pred if x[1] == "存疑待确认"})
    issues_score = 15 * ordinary["f1"] + 5 * ambiguous["f1"]
    components = {"合同要素": contract_score, "归集与匹配": grouping_score, "核算": calculation_score,
                  "问题清单": issues_score, "催缴对象": recipient_score}
    denominator = sum(m["gold_nonempty"] for m in per_field.values())
    matched = sum(r["已提交"] for r in contracts)
    warnings = []
    duplicates = _duplicates(submitted[FILES[0]], "档案号")
    if duplicates:
        warnings.append("重复档案号按官方脚本最后一行计分，不能视为多份覆盖")
    invalid_methods = sorted({str(r.get("处理方式")) for name in FILES[:2] for r in submitted[name]
                              if r.get("处理方式") not in {None, "", "人工复核", "自动通过"}})
    if invalid_methods:
        warnings.append("存在非规范处理方式；忠实使用官方“含复核则人工，否则自动”语义")
    report = {
        "source_kind": source_kind,
        "coverage_label": "提交行覆盖率（未核实发布记录）",
        "coverage": {"contracts": ratio(matched, gold_count),
                     "parcels": ratio(sum(r["已提交"] for r in parcel_rows), ledger_count)},
        "field_accuracy": {"definition": "固定标准答案非空字段为分母；缺少提交行计错；不借用未发布识别候选",
                           "all": ratio(correct, denominator), "submitted_subset": ratio(correct, submitted_nonempty),
                           "unexpected_nonempty": sum(m["unexpected_nonempty"] for m in per_field.values()),
                           "per_field": per_field},
        "whole_contract": {"key_all_correct": key_correct, "all_15_correct": all_correct,
                           "submitted_count": matched, "gold_count": gold_count,
                           "key_accuracy_submitted": key_correct / matched if matched else None},
        "automatic_contracts": {"submitted": auto_submitted, "known_wrong": auto_wrong,
                                "known_correct": auto_submitted - auto_wrong,
                                "accuracy": (auto_submitted - auto_wrong) / auto_submitted if auto_submitted else None,
                                "official_automatic_including_missing": auto_submitted + gold_count - matched},
        "automatic_parcels": {"submitted": ledger_auto, "known_wrong_arrears": ledger_auto_wrong},
        "missing_contracts": [r["档案号"] for r in contracts if not r["已提交"]],
        "missing_parcels": [r["宗地键"] for r in parcel_rows if not r["已提交"]],
        "extra_contracts": sorted(set(predicted) - set(actual)), "duplicate_contracts": duplicates,
        "grouping": {"pairs": pairing, "singles": ratio(single_count, len(singles)),
                     "registration_units": ratio(unit_correct, ledger_count)},
        "accounting": {"arrears": ratio(arrears_correct, ledger_count), "total": ratio(total_correct, ledger_count),
                       "year_2026": ratio(annual_correct, ledger_count), "recipient": ratio(recipient_correct, ledger_count)},
        "issues": {"ordinary": ordinary, "ambiguous": ambiguous,
                   "false_positive": sorted(issue_pred - issue_gold), "false_negative": sorted(issue_gold - issue_pred)},
        "score": {"total": sum(components.values()), "components": components,
                  "contract_raw_points": points, "arrears_raw_points": arrears_points},
        "automatic_policy_note": "自动期望2p−1，人工0.5；p>75%仅为计分必要条件。零观察错误不等于零风险；不据答案更改处理方式。",
        "warnings": warnings,
        "contracts": contracts, "parcels": parcel_rows, "field_errors": errors,
    }
    return report


def run_official(scorer, answers_path, payloads, computed):
    scorer = Path(scorer).resolve()
    if not scorer.is_file():
        raise ValueError(f"官方评分脚本不存在：{scorer}")
    with tempfile.TemporaryDirectory(prefix="fde-score-readonly-") as name:
        destination = Path(name)
        for filename, content in payloads.items():
            (destination / filename).write_bytes(content)
        result = subprocess.run([sys.executable, str(scorer), str(Path(answers_path).resolve()), str(destination)],
                                capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise ValueError("官方脚本执行失败：" + result.stderr[-1000:])
    match = re.search(r"总分 ([\d.]+) / 100\s*（合同要素 ([\d.]+)/30，归集与匹配 ([\d.]+)/15，核算 ([\d.]+)/30，问题清单 ([\d.]+)/20，催缴对象 ([\d.]+)/5）", result.stdout)
    if not match:
        raise ValueError("无法解析官方五项得分，停止报告而非猜测格式")
    labels = ["total", *computed["components"]]
    displayed = dict(zip(labels, map(float, match.groups()), strict=True))
    values = {"total": computed["total"], **computed["components"]}
    parity = all(math.isclose(displayed[key], float(f"{values[key]:.1f}"), abs_tol=1e-9) for key in labels)
    if not parity:
        raise ValueError(f"独立计算与官方脚本不一致：{displayed} / {values}")
    return {"verified": True, "scorer": str(scorer), "scorer_sha256": hashlib.sha256(scorer.read_bytes()).hexdigest(),
            "displayed_scores": displayed, "stdout": result.stdout}


def evaluate(answers_path, submission_path, *, scorer=DEFAULT_SCORER):
    answers, _ = load_submission(answers_path, required=True)
    submitted, payloads = load_submission(submission_path)
    report = score_rows(answers, submitted, source_kind="product_export_zip" if Path(submission_path).is_file() else "csv_directory")
    report["input"] = {"answers": str(Path(answers_path).resolve()), "submission": str(Path(submission_path).resolve()),
                       "submission_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in payloads.items()}}
    report["official"] = run_official(scorer, answers_path, payloads, report["score"])
    return report


def compare_scenarios(reports, baseline="baseline"):
    base = reports[baseline]
    deltas = {name: report["score"]["total"] - base["score"]["total"] for name, report in reports.items()}
    rows = []
    for name, report in reports.items():
        row = {"scenario": name, "score": report["score"]["total"], "delta": deltas[name],
               "contract_rows": report["coverage"]["contracts"]["correct"],
               "parcel_rows": report["coverage"]["parcels"]["correct"],
               "automatic_known_wrong": report["automatic_contracts"]["known_wrong"]}
        parts = name.split("+")
        if len(parts) > 1 and len(parts) == len(set(parts)) and all(p in reports and p != baseline for p in parts):
            row["interaction"] = deltas[name] - sum(deltas[p] for p in parts)
            row["interaction_definition"] = "组合增量减去各单项增量之和，仅描述非加性，不证明因果份额"
        rows.append(row)
    return {"baseline": baseline, "scenarios": rows,
            "note": "所有情景来自独立实际CSV/ZIP；未生成假想值或改写处理方式；不按文件数量分摊失分。"}


def _write_csv(path, rows, default_columns):
    columns = list(rows[0]) if rows else default_columns
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def write_report(report, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "score_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / "official_score.txt").write_text(report["official"]["stdout"], encoding="utf-8")
    _write_csv(destination / "合同状态.csv", report["contracts"], ["档案号"])
    _write_csv(destination / "宗地状态.csv", report["parcels"], ["宗地键"])
    _write_csv(destination / "字段差异.csv", report["field_errors"], ["档案号", "字段", "提交值", "标准答案", "原因"])
    all_fields = report["field_accuracy"]["all"]
    subset = report["field_accuracy"]["submitted_subset"]
    lines = ["# 离线评分核对", "", f"官方分数：{report['score']['total']:.4f} / 100，五项校验一致。", "",
             f"提交合同 {report['coverage']['contracts']['correct']}/{report['coverage']['contracts']['denominator']}；宗地 {report['coverage']['parcels']['correct']}/{report['coverage']['parcels']['denominator']}。覆盖率依据提交行，未独立核实发布记录。", "",
             f"全量字段正确 {all_fields['correct']}/{all_fields['denominator']}；已提交子集正确 {subset['correct']}/{subset['denominator']}。", "",
             f"已提交自动通过 {report['automatic_contracts']['submitted']} 份，其中已知错误 {report['automatic_contracts']['known_wrong']} 份。", "",
             report["automatic_policy_note"], "", "缺少合同：" + "、".join(report["missing_contracts"]),
             "", "缺少宗地：" + "、".join(report["missing_parcels"])]
    if report["warnings"]:
        lines += ["", *report["warnings"]]
    (destination / "评分核对.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--answers", type=Path, required=True)
    parser.add_argument("--submission", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scorer", type=Path, default=DEFAULT_SCORER)
    parser.add_argument("--scenario", action="append", default=[], metavar="NAME=DIR_OR_ZIP")
    args = parser.parse_args(argv)
    try:
        inputs = {"baseline": args.submission}
        for item in args.scenario:
            name, separator, path = item.partition("=")
            if not separator or not name or name in inputs or not path:
                raise ValueError("情景须为唯一的NAME=路径，baseline为保留名称")
            inputs[name] = Path(path)
        reports = {name: evaluate(args.answers, path, scorer=args.scorer) for name, path in inputs.items()}
        args.output.mkdir(parents=True, exist_ok=True)
        write_report(reports["baseline"], args.output)
        for index, (name, report) in enumerate(reports.items()):
            if name != "baseline":
                write_report(report, args.output / f"scenario-{index}")
        comparison = compare_scenarios(reports)
        (args.output / "scenario_comparison.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"score": reports["baseline"]["score"], "output": str(args.output.resolve()),
                          "scenarios": comparison["scenarios"]}, ensure_ascii=False))
    except (ValueError, OSError, KeyError, UnicodeError, csv.Error, zipfile.BadZipFile, subprocess.TimeoutExpired) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
