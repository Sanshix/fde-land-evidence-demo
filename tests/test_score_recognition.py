"""Frozen recognition diagnostics keep missing results and conflicts in their denominators."""

import csv
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("score_recognition", Path(__file__).parents[1] / "scripts/score_recognition.py")
score = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(score)


def setup_run(tmp_path, *, selected=("C-1", "C-2")):
    answers = tmp_path / "answers"
    answers.mkdir()
    rows = [{"档案号": name, **{label: "value" for label in score.FIELDS}}
            for name in ("C-1", "C-2", "C-3")]
    for row in rows:
        row.update({"土地面积": "100", "年租金小写": "120", "签订日期": "2026-01-01",
                    "租赁起始日": "2026-01-01", "租赁终止日": "2026-12-31", "计租方式": "固定", "承租人": "甲"})
    with (answers / "合同要素.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    run = tmp_path / "run"
    run.mkdir()
    (run / "documents.json").write_text(json.dumps([{"name": name, "files": ["p.jpg"]} for name in selected]))
    return run, answers


def candidate(value, **kwargs):
    return {"value": value, "model": "model-a", "kind": "ocr", "source_page_id": "p1", "raw_text": value, **kwargs}


def save_result(run, name, candidates, **extra):
    folder = run / "recognition" / name
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps({"candidates": candidates, **extra}))
    (folder / "outcome.json").write_text(json.dumps({"published": False, "state": "needs_human", "pages": 1}))


def test_absent_results_stay_in_selected_and_all_gold_denominator(tmp_path):
    run, answers = setup_run(tmp_path)
    save_result(run, "C-1", {"area": [candidate("100")], "tenant": [candidate("甲")]},
                page_coverage={"completed": ["p1"], "total": 1}, completion_status="completed")
    report = score.evaluate(run, answers)
    assert report["documents"]["with_result"] == 1
    assert report["field_accuracy"]["selected_fixed_denominator"] == {"correct": 2, "denominator": 30, "accuracy": 2 / 30}
    assert report["field_accuracy"]["all_gold_fixed_denominator"] == {"correct": 2, "denominator": 45, "accuracy": 2 / 45}
    assert report["field_accuracy"]["precision_unique_produced"]["accuracy"] == 1
    assert report["protocol_coverage"]["completed_pages"] == 1
    assert report["protocol_coverage"]["selected_pages"] == 2
    assert report["completion_states"] == {"completed": 1, "not_run": 1}
    assert report["publication"] == {"recorded_true": 0, "recorded_false": 1, "unknown": 1,
                                    "note": "不生成人工复核/自动通过标签，不计算产品发布得分或核算得分"}
    assert "score" not in report and "automatic" not in report


def test_conflicts_and_crossedout_never_choose_answer_or_current(tmp_path):
    run, answers = setup_run(tmp_path, selected=("C-1",))
    save_result(run, "C-1", {
        "area": [candidate("100", text_role="current"), candidate("200", text_role="crossed_out")],
        "tenant": [candidate("甲"), candidate("乙", model="model-b")],
        "annual": [candidate("120", kind="context_suggestion")],
    })
    report = score.evaluate(run, answers)
    assert report["field_accuracy"]["selected_fixed_denominator"]["correct"] == 0
    assert report["field_accuracy"]["coverage_selected"]["correct"] == 0
    assert report["field_accuracy"]["per_field"]["土地面积"]["withheld_conflicts"] == 1
    assert report["whole_contract"]["key_all_correct"] == 0
    assert {r["候选状态"] for r in report["errors"] if r["字段"] in {"土地面积", "承租人"}} == {"withheld_conflict", "withheld_alteration"}


def test_fullwidth_numeric_duplicates_are_one_unique_candidate(tmp_path):
    run, answers = setup_run(tmp_path, selected=("C-1",))
    save_result(run, "C-1", {"area": [candidate("１００.００平方米"), candidate("100", model="model-b")]})
    report = score.evaluate(run, answers)
    assert report["field_accuracy"]["precision_unique_produced"] == {"correct": 1, "denominator": 1, "accuracy": 1}
    assert report["field_rows"][8]["字段"] == "土地面积"
    assert report["field_rows"][8]["唯一候选"] == "100"


def test_cli_outputs_field_diagnostics_without_product_or_model_imports(tmp_path):
    run, answers = setup_run(tmp_path)
    out = tmp_path / "diagnostic"
    before = (run / "documents.json").read_bytes()
    assert score.main(["--run", str(run), "--answers", str(answers), "--output", str(out)]) == 0
    assert (out / "recognition_score.json").is_file()
    assert (out / "识别字段诊断.md").is_file()
    assert (run / "documents.json").read_bytes() == before
    script = Path(score.__file__)
    process = subprocess.run([sys.executable, "-c", f"import runpy,sys;runpy.run_path({str(script)!r});assert 'app.providers' not in sys.modules;assert 'app.store' not in sys.modules;assert 'httpx' not in sys.modules"],
                             capture_output=True, text=True, check=False)
    assert process.returncode == 0, process.stderr
    assert not (run / "data").exists()


def test_duplicate_or_missing_manifest_rejected(tmp_path):
    run, answers = setup_run(tmp_path, selected=("C-1", "C-1"))
    with pytest.raises(ValueError, match="重复档案号"):
        score.evaluate(run, answers)
    (run / "documents.json").unlink()
    with pytest.raises(ValueError, match="documents.json"):
        score.evaluate(run, answers)
