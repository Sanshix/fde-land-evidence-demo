"""Independent recovery acceptance. Synthetic inputs; no cloud or product database writes."""

import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from test_cloud import cloud as cloud_fixture
from test_domain import fields

from app.cloud_attempts import AttemptRunner
from app.cloud_cascade import run_document_cascade
from app.domain import FIELDS
from app.providers import CloudProvider, parse_reply
from app.triage import triage

cloud = cloud_fixture


def chat(fields=None, *, raw=None, finish="stop"):
    return httpx.Response(200, json={
        "id": "synthetic-response", "model": "synthetic-model",
        "choices": [{"message": {"content": raw if raw is not None else json.dumps({"fields": fields or [], "warnings": []})},
                     "finish_reason": finish}], "usage": {"completion_tokens": 10},
    })


def field(key="area", value="10", **extras):
    return {"field": key, "value": value, "raw_text": f"{key}: {value}",
            "bbox": [.1, .2, .5, .3], **extras}


@pytest.mark.parametrize("invalid_page", [0, 3, -1, True, False, 1.0, "1"])
def test_short_page_reference_rejects_only_bad_candidate(cloud, invalid_page):
    page, config = cloud
    second = {**page, "id": "independent-long-id-page-two"}
    body_seen = []

    def handler(request):
        body_seen.append(json.loads(request.content))
        return chat([field(page=1), field("tenant", "must-not-adopt", page=invalid_page)])

    result = CloudProvider(config["models"]["primary"], httpx.MockTransport(handler)).identify_document([page, second])
    assert result["candidates"]["area"][0]["source_page_id"] == page["id"]
    assert not result["candidates"]["tenant"]
    assert len(result["rejected"]) == 1
    prompt = json.dumps(body_seen[0]["messages"], ensure_ascii=False)
    assert page["id"] not in prompt and second["id"] not in prompt


def test_cross_document_or_conflicting_legacy_page_is_never_repaired(cloud):
    page, config = cloud
    wrong = page["id"] + "-one-character-off"
    result = CloudProvider(config["models"]["primary"], httpx.MockTransport(lambda _: chat([
        field(page=1), field("tenant", "bad", page_id=wrong),
        field("phone", "10000000000", page=1, page_id="outside-this-document"),
        field("contract_no", "legacy-exact", page_id=page["id"]),
    ]))).identify_document([page])
    assert len(result["rejected"]) == 2
    assert not result["candidates"]["tenant"] and not result["candidates"]["phone"]
    assert result["candidates"]["contract_no"][0]["source_page_id"] == page["id"]


@pytest.mark.parametrize("finish", ["length", "stop"])
def test_partial_json_is_retained_but_never_a_candidate(cloud, tmp_path, finish):
    page, config = cloud
    config["models"] = {"primary": config["models"]["primary"]}
    partial = '{"fields":[{"field":"area","value":"10"'
    result = run_document_cascade([page], tmp_path / finish, strategy="primary", config=config,
        transport=httpx.MockTransport(lambda _: chat(raw=partial, finish=finish)))
    assert not result["candidates"]["area"]
    assert result["completion_status"] == "failed"
    evidence_paths = list((tmp_path / finish).glob("*.response.json"))
    assert evidence_paths
    evidence = json.loads(evidence_paths[0].read_text())
    assert evidence["raw_response_text"] == partial and evidence["finish_reason"] == finish


def test_failed_multi_page_read_recovers_once_per_page_and_reuses_completed_pages(cloud, tmp_path):
    page, config = cloud
    pages = [page, {**page, "id": "second-page"}]
    config["models"] = {"primary": config["models"]["primary"]}
    calls = []

    def handler(request):
        body = json.loads(request.content)
        image_count = sum(c.get("type") == "image_url" for m in body["messages"]
                          for c in m["content"] if isinstance(m["content"], list))
        calls.append(image_count)
        if image_count == 2:
            raise httpx.ReadTimeout("synthetic ambiguous multi-page timeout")
        return chat([field()])

    transport = httpx.MockTransport(handler)
    first = run_document_cascade(pages, tmp_path / "recovery", config=config, transport=transport)
    assert calls == [2, 1, 1]
    assert {c["source_page_id"] for c in first["candidates"]["area"]} == {p["id"] for p in pages}
    assert first["completion_status"] == "completed"
    second = run_document_cascade(pages, tmp_path / "recovery", config=config, transport=transport, retry_failed=True)
    assert calls == [2, 1, 1]
    assert all(row["stages"][0]["mode"] == "resumed_evidence" for row in second["pages"])
    assert first["state"] == second["state"] == "needs_human"


def test_unknown_attempt_needs_explicit_resubmit_even_if_retry_failed_requested(tmp_path):
    profile = {"provider": "stub", "model_id": "stub", "base_url": "https://mock.invalid"}
    calls = []

    def ambiguous(_):
        calls.append("sent")
        raise httpx.ReadTimeout("synthetic")

    first = AttemptRunner(tmp_path, 2, transport=object()).call("request", "primary", profile, ambiguous)
    assert first["outcome_unknown"]
    repeated = AttemptRunner(tmp_path, 2, transport=object(), retry_failed=True).call("request", "primary", profile, ambiguous)
    assert calls == ["sent"] and repeated["new_calls"] == 0
    AttemptRunner(tmp_path, 2, transport=object(), retry_unknown=True).call("request", "primary", profile, ambiguous)
    assert calls == ["sent", "sent"]
    assert len(list(tmp_path.glob("request.attempt-[0-9].json"))) == 2


def test_late_response_cannot_publish_cache_or_change_unknown_outcome(tmp_path):
    profile = {"provider": "stub", "model_id": "stub", "base_url": "https://mock.invalid"}
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow(_):
        entered.set()
        try:
            assert release.wait(2)
            return {"candidates": {k: [] for k in FIELDS}, "response_id": "late-response"}
        finally:
            finished.set()

    try:
        started = time.monotonic()
        result = AttemptRunner(tmp_path, .15, transport=object()).call("slow", "primary", profile, slow)
        assert entered.is_set() and time.monotonic() - started < .8
        assert result["outcome_unknown"]
        before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    finally:
        release.set()
        assert finished.wait(2)
    assert not (tmp_path / "slow.json").exists()
    assert before == {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}


def test_global_request_ceiling_is_two_across_documents(tmp_path):
    profile = {"provider": "stub", "model_id": "stub", "base_url": "https://mock.invalid"}
    lock = threading.Lock()
    release, two_entered = threading.Event(), threading.Event()
    active = maximum = total = 0

    def request(_):
        nonlocal active, maximum, total
        with lock:
            active += 1
            total += 1
            maximum = max(maximum, active)
            if active == 2:
                two_entered.set()
        try:
            assert release.wait(2)
            return {"response_id": "synthetic", "candidates": {}}
        finally:
            with lock:
                active -= 1

    def document(number):
        return AttemptRunner(tmp_path / str(number), 3, transport=object()).call("request", "primary", profile, request)

    with ThreadPoolExecutor(max_workers=4) as callers:
        futures = [callers.submit(document, n) for n in range(4)]
        try:
            assert two_entered.wait(1)
            with lock:
                assert active == 2 and total == 2
        finally:
            release.set()
        results = [f.result(timeout=3) for f in futures]
    assert maximum == 2 and total == 4 and all(r["status"] == "completed" for r in results)


def review_document(*, change=False):
    values = ({"doc_type": "变更", "contract_no": "change-1", "tenant": "测试承租人", "parcel_no": "9-9-999"}
              if change else fields(doc_type="新签", contract_no="contract-1"))
    candidates = {key: [{"value": value, "model": model, "provider": "stub", "kind": "ocr",
                        "source_page_id": "page", "raw_text": value, "bbox": [1, 2, 40, 20], "score": .99}
                       for model in ("model-a", "model-b")] for key, value in values.items()}
    return {"state": "needs_human", "pages": [{"id": "page", "width": 200, "height": 200}],
            "result": {"candidates": candidates, "risks": []}, "intake": {}}


def test_text_only_corroboration_does_not_poison_good_source_anchor():
    doc = review_document()
    for rows in doc["result"]["candidates"].values():
        rows[1].update(bbox=None, score=None, score_meaning="not_provided")
    before = copy.deepcopy(doc)
    result = triage(doc)
    assert result["eligible"], result["reasons"]
    assert result["evidence_status"]["area"]["unknown_score_paths"] == 1
    assert result["citation_details"]["area"][0]["bbox"] == [1, 2, 40, 20]
    assert doc == before  # Re-routing must not rewrite stored extraction evidence.


@pytest.mark.parametrize("mutation", ["no-anchor", "real-low-score", "conflict", "outside-page", "alteration"])
def test_unsafe_evidence_remains_ineligible(mutation):
    doc = review_document()
    rows = doc["result"]["candidates"]["area"]
    if mutation == "no-anchor":
        for row in rows:
            row.update(bbox=None, score=None, score_meaning="not_provided")
    elif mutation == "real-low-score":
        rows[1]["score"] = .4
    elif mutation == "conflict":
        rows[1]["value"] = "11"
    elif mutation == "outside-page":
        for row in rows:
            row["source_page_id"] = "other-document-page"
    else:
        rows[1]["text_role"] = "crossed_out"
    assert not triage(doc)["eligible"]


def test_equivalent_fullwidth_numeric_and_dates_agree_without_losing_raw_evidence():
    doc = review_document()
    doc["result"]["candidates"]["area"][1]["value"] = "１０.００平方米"
    doc["result"]["candidates"]["start"][1]["value"] = "２０２６年１月１日"
    result = triage(doc)
    assert result["eligible"], result["reasons"]
    assert result["fields"]["area"] == "10" and result["fields"]["start"] == "2026-01-01"
    assert doc["result"]["candidates"]["area"][1]["value"] == "１０.００平方米"


def test_change_agreement_does_not_require_contract_term_or_rent():
    result = triage(review_document(change=True))
    assert result["eligible"], result["reasons"]
    assert not any(k in result["fields"] for k in ("annual", "start", "end", "mode", "area"))


def test_boolean_coordinates_are_not_source_regions(cloud):
    page, config = cloud
    candidate = parse_reply({"fields": [field(bbox=[False, .1, True, .5])]}, page, (0, 0, 200, 200), config["models"]["primary"])["area"][0]
    assert candidate["bbox"] is None


def test_baidu_submitted_task_failure_stays_unknown_and_retains_task_id(tmp_path):
    from app.baidu_ocr import BaiduOCRError

    profile = {"provider": "baidu_ai_ocr", "model_id": "paddleocr-vl-service", "base_url": "https://aip.baidubce.com"}

    def interrupted_query(_):
        raise BaiduOCRError("query", "deadline_exceeded", "submitted-task-id")

    result = AttemptRunner(tmp_path, 2, transport=object()).call("baidu", "secondary", profile, interrupted_query)
    assert result["outcome_unknown"]
    assert result["task_id"] == "submitted-task-id"


def test_page_fallback_cache_is_stable_across_process_hash_seeds(cloud, tmp_path):
    import os
    import subprocess
    import sys

    page, config = cloud
    folder = tmp_path / "restart"
    config_path = tmp_path / "config.json"
    page_path = tmp_path / "page.json"
    config_path.write_text(json.dumps(config))
    page_path.write_text(json.dumps(page))
    code = '''
import json,sys
from pathlib import Path
import httpx
from app.cloud_cascade import run_cloud_cascade
folder, config_path, page_path = map(Path, sys.argv[1:])
config=json.loads(config_path.read_text());page=json.loads(page_path.read_text());calls=[]
def handler(request):
    calls.append(json.loads(request.content)['model'])
    return httpx.Response(200,json={'model':'stub','choices':[{'message':{'content':json.dumps({'fields':[{'field':'area','value':'10','raw_text':'area 10','bbox':[.1,.1,.3,.2],'score':.99,'reason':'synthetic'}]})}}]})
run_cloud_cascade([page],folder,config=config,transport=httpx.MockTransport(handler))
print(json.dumps(calls))
'''
    results = []
    for seed in ("1", "20"):
        run = subprocess.run([sys.executable, "-c", code, str(folder), str(config_path), str(page_path)],
                             capture_output=True, text=True, timeout=10,
                             env={**os.environ, "PYTHONHASHSEED": seed, "FDE_TEST_KEY": "synthetic-test-key"})
        assert run.returncode == 0, run.stderr
        results.append(json.loads(run.stdout))
    assert results[0] == ["stub-primary", "stub-secondary", "stub-context"]
    assert results[1] == []


def test_document_global_fields_do_not_trigger_unnecessary_blank_page_fallback(cloud, tmp_path, monkeypatch):
    from app.cloud_cascade import run_cloud_cascade

    page, config = cloud
    pages = [page, {**page, "id": "readable-blank-page-two"}]
    values = fields(doc_type="新签", contract_no="synthetic-complete")
    all_fields = [field(key, value, score=.99) for key, value in values.items()]
    calls = []
    primary_count = 0
    monkeypatch.setattr("app.cloud_cascade.quality", lambda _: {"warnings": []})

    def handler(request):
        nonlocal primary_count
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "stub-primary":
            primary_count += 1
            return chat(all_fields if primary_count == 1 else [])
        return chat(all_fields)

    result = run_cloud_cascade(pages, tmp_path / "global-fields", config=config,
                               transport=httpx.MockTransport(handler))
    assert calls == ["stub-primary", "stub-primary", "stub-secondary"]
    assert not any(stage["stage"] != "primary" for stage in result["pages"][1]["stages"])
    assert triage({"state": "needs_human", "pages": pages, "result": result, "intake": {}})["eligible"]


def test_failed_page_is_still_recovered_when_other_page_already_supplies_all_fields(cloud, tmp_path, monkeypatch):
    from app.cloud_cascade import run_cloud_cascade

    page, config = cloud
    pages = [page, {**page, "id": "failed-page-two"}]
    values = fields(doc_type="新签", contract_no="synthetic-complete")
    all_fields = [field(key, value, score=.99) for key, value in values.items()]
    calls = []
    primary_count = 0
    monkeypatch.setattr("app.cloud_cascade.quality", lambda _: {"warnings": []})

    def handler(request):
        nonlocal primary_count
        model = json.loads(request.content)["model"]
        calls.append(model)
        if model == "stub-primary":
            primary_count += 1
            if primary_count == 2:
                raise httpx.ReadTimeout("page two not read")
        # Page two could hold signatures/annotations even if fields exist on page one.
        return chat(all_fields)

    result = run_cloud_cascade(pages, tmp_path / "failed-page", config=config,
                               transport=httpx.MockTransport(handler))
    page_two_stages = result["pages"][1]["stages"]
    assert any(s["stage"] != "primary" and s["status"] == "completed" for s in page_two_stages)
    assert result["page_coverage"]["incomplete"] == []
    count = len(calls)
    run_cloud_cascade(pages, tmp_path / "failed-page", config=config,
                      transport=httpx.MockTransport(handler), retry_failed=True)
    assert len(calls) == count


def test_ali_coordinates_only_response_is_retained_as_failed_text_read(cloud, tmp_path):
    page, config = cloud
    profile = {**config["models"]["primary"], "provider": "aliyun", "model_id": "qwen-vl-ocr-2025-11-20",
               "response_format": "ocr_text"}
    observed = []
    raw = "822,46,13,73,90\n748,44,11,33,90"

    def handler(request):
        body = json.loads(request.content)
        observed.append(body)
        return chat(raw=raw)

    transport = httpx.MockTransport(handler)
    result = AttemptRunner(tmp_path, 2, transport=transport).call(
        "ali-coordinates", "primary", profile,
        lambda timeout: CloudProvider(profile, transport).identify(page, timeout=timeout))
    assert result["status"] == "failed" and not result.get("candidates")
    evidence = json.loads((tmp_path / result["response_evidence"]).read_text())
    assert evidence["raw_response_text"] == raw
    contents = observed[0]["messages"][0]["content"]
    assert [part["type"] for part in contents] == ["image_url", "text"]
    assert contents[1]["text"].startswith("Please output only the text content")


def test_structured_prompt_does_not_invent_zero_confidence_when_provider_omits_score(cloud):
    page, config = cloud
    observed = []

    def handler(request):
        body = json.loads(request.content)
        observed.append(body)
        return chat([field()])

    result = CloudProvider(config["models"]["primary"], httpx.MockTransport(handler)).identify(page)
    candidate = result["candidates"]["area"][0]
    assert candidate["score"] is None and candidate["score_meaning"] == "not_provided"
    prompt = json.loads(observed[0]["messages"][1]["content"][0]["text"])
    assert "score" not in prompt["output_schema"]["fields"][0]


@pytest.mark.parametrize("cause", ["conflict", "low_score"])
def test_complete_independent_readers_route_to_human_instead_of_third_model_vote(cloud, tmp_path, monkeypatch, cause):
    from app.cloud_cascade import run_cloud_cascade

    page, config = cloud
    config["models"]["third"] = {**config["models"]["primary"], "model_id": "must-not-vote"}
    values = fields(doc_type="新签", contract_no="synthetic-complete")
    calls = []
    monkeypatch.setattr("app.cloud_cascade.quality", lambda _: {"warnings": ["synthetic blur warning"]})

    def handler(request):
        model = json.loads(request.content)["model"]
        calls.append(model)
        observed = [field(key, value, score=.99) for key, value in values.items()]
        if model == "stub-secondary":
            area = next(row for row in observed if row["field"] == "area")
            area.update(value="11", raw_text="area 11") if cause == "conflict" else area.update(score=.4)
        return chat(observed)

    result = run_cloud_cascade([page], tmp_path / cause, config=config, transport=httpx.MockTransport(handler))
    assert calls == ["stub-primary", "stub-secondary"]
    routed = triage({"state": "needs_human", "pages": [page], "result": result, "intake": {}})
    assert not routed["eligible"]
    expected = "disagreement" if cause == "conflict" else "low_score"
    assert any(blocker["code"] == expected for blocker in routed["blockers"])


def test_provider_warning_still_requires_manual_review_after_consistent_values(cloud, tmp_path, monkeypatch):
    from app.cloud_cascade import run_cloud_cascade

    page, config = cloud
    values = fields(doc_type="新签", contract_no="synthetic-complete")
    all_fields = [field(key, value, score=.99) for key, value in values.items()]
    monkeypatch.setattr("app.cloud_cascade.quality", lambda _: {"warnings": []})

    def handler(request):
        warning = [{"type": "alteration", "reason": "签章区域需核对"}] if json.loads(request.content)["model"] == "stub-primary" else []
        return chat(raw=json.dumps({"fields": all_fields, "warnings": warning}))

    result = run_cloud_cascade([page], tmp_path / "warning", config=config, transport=httpx.MockTransport(handler))
    routed = triage({"state": "needs_human", "pages": [page], "result": result, "intake": {}})
    assert not routed["eligible"]
    assert any(blocker["code"] == "source_warning" for blocker in routed["blockers"])


@pytest.mark.parametrize("frame", ["exact", "mismatched", "rotated"])
def test_layout_region_is_provider_block_not_fabricated_cell_confidence(frame):
    from app.ocr_layout import baidu_candidates

    document_page = {"meta": {"page_width": 200, "page_height": 200}, "layouts": [
        {"type": "table", "position": [10, 20, 100, 50], "score": .999,
         "table_html": "<table><tr><td>土地面积</td><td>10平方米</td><td>用途</td><td>住宅</td></tr></table>"},
    ]}
    if frame == "mismatched":
        document_page["meta"]["page_width"] = 400
    elif frame == "rotated":
        document_page["page_angle"] = 90
    candidates, diagnostics = baidu_candidates({"pages": [document_page]}, {"id": "submitted-image"},
        {"model_id": "baidu-layout-stub", "provider": "stub"}, [0, 0, 200, 200])
    area = candidates["area"][0]
    assert area["value"] == "10" and area["score"] is None and area["score_meaning"] == "not_provided"
    if frame == "exact":
        assert area["bbox"] == candidates["use"][0]["bbox"]
        assert area["bbox"] == pytest.approx([10, 20, 110, 70])
        assert area["evidence_status"] == "provider_block_needs_human"
        assert diagnostics["coordinate_frame_verified"]
    else:
        assert area["bbox"] is None and area["evidence_status"] == "missing_region"
        assert not diagnostics["coordinate_frame_verified"]


def test_contract_type_from_title_retains_visible_source_not_an_invented_caption():
    from app.extraction import prose_candidates

    actual_title = "国有建设用地使用权租赁合同"
    document = prose_candidates([{"text": actual_title, "locator": "line-1", "box": [1, 2, 100, 20]}],
                                 "page", "stub", "stub")
    candidate = document["doc_type"][0]
    assert candidate["value"] == "新签"
    assert candidate["raw_text"] == actual_title
    referenced_only = prose_candidates([{"text": "按照原租赁合同约定支付租金。", "locator": "line-2"}],
                                        "page", "stub", "stub")
    assert referenced_only["doc_type"] == []


def test_future_amendment_clause_does_not_turn_current_contract_into_change_agreement():
    from app.extraction import prose_candidates

    observed = prose_candidates([
        {"text": "国有建设用地使用权租赁合同", "locator": "title"},
        {"text": "租期届满后，如需续期，应另行签订变更协议。", "locator": "future-clause"},
    ], "page", "stub", "stub")
    assert not any(c["value"] in {"变更", "续期"} for c in observed["doc_type"])


def test_layout_future_clause_is_not_document_type_evidence():
    from app.ocr_layout import baidu_candidates

    parsed, _ = baidu_candidates({"pages": [{"meta": {"page_width": 200, "page_height": 200}, "layouts": [
        {"type": "doc_title", "text": "国有建设用地使用权租赁合同", "position": [1, 1, 150, 20]},
        {"type": "text", "text": "租期届满后，如需续期，应另行签订变更协议。", "position": [1, 30, 150, 20]},
    ]}]}, {"id": "page"}, {"model_id": "stub", "provider": "stub"}, [0, 0, 200, 200])
    assert not any(c["value"] in {"变更", "续期"} for c in parsed["doc_type"])
