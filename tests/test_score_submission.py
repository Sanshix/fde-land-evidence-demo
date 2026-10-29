"""Fixed-denominator diagnostics and exact official-score parity; no recognition or database."""

import csv
import importlib.util
import json
import shutil
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("score_submission", Path(__file__).parents[1] / "scripts/score_submission.py")
score = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(score)
ANSWERS = Path(__file__).parents[2] / "练习集/08_标准答案"


def fixture_rows():
    contracts = [{**dict.fromkeys(score.FIELDS, ""), "档案号": f"C-{i:03}", "合同编号": f"合同{i}",
                  "合同类型": "新签", "签订日期": "2026-01-01", "承租人": f"承租人{i}", "证件号码": "ABC123",
                  "联系电话": "10000000000", "宗地号": str(i), "坐落": f"测试街{i}号", "土地面积": "100",
                  "用途": "住宅", "租赁起始日": "2026-01-01", "租赁终止日": "2026-12-31", "计租方式": "固定",
                  "年租金小写": "100", "年租金大写": "壹佰元整"} for i in range(50)]
    groups = [contracts[i:i + 2] for i in range(0, 40, 2)] + [[r] for r in contracts[40:]]
    ledgers = [{"宗地键": rows[-1]["档案号"], "档案号列表": ";".join(r["档案号"] for r in rows),
                "不动产单元号": str(i), "欠缴金额": "100", "累计应缴": "100", "2026年度应缴": "100", "催缴对象": "测试人"}
               for i, rows in enumerate(groups)]
    issues = [{"档案号": "C-000", "问题类型": "合同缺页"}, {"档案号": "C-001", "问题类型": "存疑待确认"}]
    return dict(zip(score.FILES, [contracts, ledgers, issues], strict=True))


def submission(gold, count=50, *, manual=True):
    submitted = deepcopy(gold)
    submitted[score.FILES[0]] = submitted[score.FILES[0]][:count]
    for name in score.FILES[:2]:
        for row in submitted[name]:
            row["处理方式"] = "人工复核" if manual else "自动通过"
    return submitted


def write_rows(folder, rows):
    folder.mkdir()
    for name, values in rows.items():
        with (folder / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(values[0]) if values else ["档案号", "问题类型"])
            writer.writeheader()
            writer.writerows(values)
    return folder


def test_manual_cliff_and_full_manual_ceiling():
    gold = fixture_rows()
    at_33 = score.score_rows(gold, submission(gold, 33))
    at_34 = score.score_rows(gold, submission(gold, 34))
    full = score.score_rows(gold, submission(gold))
    assert at_33["score"]["components"]["合同要素"] == 0
    assert at_34["score"]["components"]["合同要素"] == .6
    assert full["score"]["components"]["合同要素"] == 15
    assert full["score"]["components"]["核算"] == 22.5
    assert full["score"]["total"] == 77.5
    assert full["coverage"]["parcels"]["denominator"] == 30


def test_nonempty_gold_denominator_fixed_and_blank_hallucinations_separate():
    gold = fixture_rows()
    gold[score.FILES[0]][0]["联系电话"] = ""
    partial = submission(gold, 21)
    partial[score.FILES[0]][0]["联系电话"] = "invented"
    result = score.score_rows(gold, partial)
    assert result["field_accuracy"]["all"]["denominator"] == 749
    assert result["field_accuracy"]["submitted_subset"]["denominator"] == 314
    assert result["field_accuracy"]["all"]["accuracy"] == 314 / 749
    assert result["field_accuracy"]["unexpected_nonempty"] == 1
    assert result["field_accuracy"]["per_field"]["联系电话"]["official_denominator"] == 50
    assert result["automatic_contracts"]["submitted"] == 0
    assert result["automatic_contracts"]["known_wrong"] == 0
    assert result["automatic_contracts"]["official_automatic_including_missing"] == 29


def test_actual_automatic_errors_penalized_without_relabeling():
    gold = fixture_rows()
    submitted = submission(gold, manual=False)
    submitted[score.FILES[0]][0]["承租人"] = "错误主体"
    result = score.score_rows(gold, submitted)
    assert result["automatic_contracts"]["submitted"] == 50
    assert result["automatic_contracts"]["known_wrong"] == 1
    assert result["score"]["components"]["合同要素"] == 28.8
    assert submitted[score.FILES[0]][0]["处理方式"] == "自动通过"


def test_parcel_matched_by_latest_key_in_archive_list_not_row_key():
    gold = fixture_rows()
    submitted = submission(gold)
    first = submitted[score.FILES[1]][0]
    first["宗地键"] = "a-different-label"
    result = score.score_rows(gold, submitted)
    assert result["coverage"]["parcels"]["correct"] == 30
    assert result["parcels"][0]["已知材料组完整"]
    first["档案号列表"] = "C-000"
    result = score.score_rows(gold, submitted)
    assert result["missing_parcels"] == ["C-001"]


def test_zip_read_safely_without_extraction_and_reject_unsafe_paths(tmp_path):
    rows = fixture_rows()
    folder = write_rows(tmp_path / "csv", submission(rows))
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        for name in score.FILES:
            stream.write(folder / name, name)
    loaded, _ = score.load_submission(archive)
    assert len(loaded[score.FILES[0]]) == 50
    assert not (tmp_path / "合同要素.csv").exists()
    with zipfile.ZipFile(archive, "a") as stream:
        stream.writestr("../outside.txt", "malicious")
    with pytest.raises(ValueError, match="越界"):
        score.load_submission(archive)
    assert not (tmp_path / "outside.txt").exists()


def test_scenario_comparison_retains_nonadditivity():
    gold = fixture_rows()
    reports = {name: score.score_rows(gold, submission(gold, count))
               for name, count in [("baseline", 21), ("A", 33), ("B", 34), ("A+B", 50)]}
    result = score.compare_scenarios(reports)
    combo = next(r for r in result["scenarios"] if r["scenario"] == "A+B")
    assert combo["delta"] == 15
    assert combo["interaction"] == pytest.approx(14.4)
    assert "因果份额" in combo["interaction_definition"]


@pytest.mark.skipif(not ANSWERS.is_dir(), reason="本机没有练习集")
def test_actual_exercise_731_denominator_and_official_parity(tmp_path):
    gold, _ = score.load_submission(ANSWERS, required=True)
    submitted = write_rows(tmp_path / "submission", submission(gold))
    result = score.evaluate(ANSWERS, submitted)
    assert result["field_accuracy"]["all"] == {"correct": 731, "denominator": 731, "accuracy": 1.0}
    assert result["score"]["total"] == 77.5
    assert result["official"]["verified"]
    assert result["official"]["displayed_scores"]["total"] == 77.5
    score.write_report(result, tmp_path / "report")
    assert json.loads((tmp_path / "report/score_report.json").read_text())["score"]["total"] == 77.5


def test_cli_scores_two_real_submissions_without_modifying_inputs(tmp_path):
    gold = fixture_rows()
    answers = write_rows(tmp_path / "answers", gold)
    first = write_rows(tmp_path / "first", submission(gold, 33))
    second = write_rows(tmp_path / "second", submission(gold, 34))
    before = {str(p): p.read_bytes() for folder in (first, second, answers) for p in folder.iterdir()}
    # Use an exact copy of the official scorer against self-contained synthetic fixtures.
    scorer = tmp_path / "score.py"
    shutil.copy2(score.DEFAULT_SCORER, scorer)
    assert score.main(["--answers", str(answers), "--submission", str(first), "--output", str(tmp_path / "report"),
                       "--scorer", str(scorer), "--scenario", f"recovery={second}"]) == 0
    compare = json.loads((tmp_path / "report/scenario_comparison.json").read_text())
    assert compare["scenarios"][1]["delta"] == pytest.approx(.6)
    assert all(Path(name).read_bytes() == value for name, value in before.items())


def test_duplicate_rows_are_not_counted_as_coverage():
    gold = fixture_rows()
    submitted = submission(gold, 21)
    submitted[score.FILES[0]].append(deepcopy(submitted[score.FILES[0]][0]))
    report = score.score_rows(gold, submitted)
    assert report["coverage"]["contracts"]["correct"] == 21
    assert report["duplicate_contracts"] == ["C-000"]
    assert report["warnings"]


@pytest.mark.skipif(not ANSWERS.is_dir(), reason="本机没有练习集")
@pytest.mark.parametrize("step", [2, 3, 5])
def test_official_parity_with_missing_manual_auto_and_business_errors(tmp_path, step):
    gold, _ = score.load_submission(ANSWERS, required=True)
    submitted = submission(gold, manual=False)
    submitted[score.FILES[0]] = [r for i, r in enumerate(submitted[score.FILES[0]]) if i % step]
    for i, row in enumerate(submitted[score.FILES[0]]):
        if i % 3 == 0:
            row["处理方式"] = "人工复核"
        if i % 5 == 0:
            row["承租人"] = "错误的模拟承租人"
    submitted[score.FILES[1]] = submitted[score.FILES[1]][1:]
    for i, row in enumerate(submitted[score.FILES[1]]):
        if i % 3 == 0:
            row["处理方式"] = "人工复核"
        if i % 4 == 0:
            row["欠缴金额"] = "-12345"
            row["不动产单元号"] = "错误编号"
        if i % 5 == 0:
            row["累计应缴"] = "0"
            row["2026年度应缴"] = "0"
            row["催缴对象"] = "错误催缴人"
    submitted[score.FILES[2]] = submitted[score.FILES[2]][::step] + [{"档案号": "EXTRA", "问题类型": "存疑待确认", "说明": "合成误报"}]
    folder = write_rows(tmp_path / "submission", submitted)
    report = score.evaluate(ANSWERS, folder)
    assert report["official"]["verified"]
    assert report["field_accuracy"]["all"]["denominator"] == 731
    assert report["automatic_contracts"]["known_wrong"] > 0
    assert report["automatic_parcels"]["known_wrong_arrears"] > 0
