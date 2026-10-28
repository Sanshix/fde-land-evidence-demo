"""Cloud protocol mocks ONLY. These tests make no model accuracy claims."""

import hashlib
import json

import httpx
import pytest
from PIL import Image

from app.cloud_cascade import run_cloud_cascade
from app.providers import ProviderUnavailable, approved_input, model_profile, parse_reply


@pytest.fixture
def cloud(tmp_path, monkeypatch):
    monkeypatch.setenv("FDE_TEST_KEY", "synthetic-test-key")
    image = tmp_path / "synthetic.png"
    Image.new("RGB", (200, 200), "white").save(image)
    page = {"id": "test-page", "path": str(image), "source": str(image)}
    p = {
        "provider": "TEST-STUB",
        "base_url": "https://mock.invalid/v1",
        "model_id": "stub-primary",
        "api_key_env": "FDE_TEST_KEY",
        "authorized": True,
    }
    config = {
        "enabled": True,
        "authorized_sha256": [hashlib.sha256(image.read_bytes()).hexdigest()],
        "models": {
            "primary": p,
            "secondary": p | {"model_id": "stub-secondary"},
            "context": p | {"model_id": "stub-context"},
        },
    }
    return page, config


def response(value="123.45"):
    fields = [
        {
            "field": "area",
            "value": value,
            "raw_text": "面积" + value,
            "bbox": [0.1, 0.2, 0.4, 0.3],
            "score": 0.99,
            "reason": "第1页面积行",
            "status": "candidate",
        }
    ]
    return httpx.Response(
        200,
        json={
            "id": "test-response",
            "model": "STUB-NOT-REAL-MODEL",
            "choices": [{"message": {"content": json.dumps({"fields": fields})}}],
            "usage": {"total_tokens": 123},
        },
    )


def test_unconfigured_never_invokes_any_model(tmp_path):
    result = run_cloud_cascade([], tmp_path, config={"enabled": False})
    assert result["mode"] == "unconfigured"
    assert result["external_calls"] == 0 and result["state"] == "needs_human"


def test_scope_and_config_block_before_egress(cloud):
    page, cfg = cloud
    with pytest.raises(ProviderUnavailable):
        approved_input(page, {"authorized_scope": "fictional_exercise"})
    with pytest.raises(ProviderUnavailable):
        model_profile(cfg | {"enabled": False}, "primary")
    cfg["models"]["primary"]["base_url"] = "http://mock.invalid"
    with pytest.raises(ProviderUnavailable):
        model_profile(cfg, "primary")


def test_cascade_conflict_context_and_resume(cloud, tmp_path):
    page, cfg = cloud
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body["model"])
        return response("123.45" if body["model"] == "stub-primary" else "128.45")

    transport = httpx.MockTransport(handler)
    result = run_cloud_cascade([page], tmp_path / "run", config=cfg, transport=transport)
    assert len(calls) == 3
    assert result["mode"] == "test_stub" and result["external_calls"] == 0
    assert result["third_model"] == "not_configured"
    assert any(r["code"] == "disagreement" for r in result["risks"])
    cs = result["candidates"]["area"]
    assert cs[0]["bbox"] == [20, 40, 80, 60]
    assert any(c["kind"] == "context_suggestion" for c in cs)
    assert all(not c["can_publish"] and not c["verified"] for c in cs)
    repeated = run_cloud_cascade([page], tmp_path / "run", config=cfg, transport=transport)
    assert len(calls) == 3
    assert repeated["pages"][0]["stages"][0]["mode"] == "resumed_evidence"


def test_same_model_not_independent_and_timeout_safe(cloud, tmp_path):
    page, cfg = cloud
    cfg["models"]["secondary"] = cfg["models"]["primary"].copy()
    cfg["models"]["context"] = None
    result = run_cloud_cascade(
        [page], tmp_path / "same", config=cfg, transport=httpx.MockTransport(lambda _: response())
    )
    assert any(s["status"] == "skipped_same_model" for s in result["pages"][0]["stages"])

    def fail(_):
        raise httpx.ReadTimeout("mock timeout")

    result = run_cloud_cascade([page], tmp_path / "timeout", config=cfg, transport=httpx.MockTransport(fail))
    assert result["state"] == "needs_human"
    assert result["pages"][0]["stages"][0]["status"] == "failed"
    assert not list((tmp_path / "timeout").glob("[0-9a-f]" * 64 + ".json"))


def test_context_without_evidence_is_rejected(cloud):
    page, cfg = cloud
    result = parse_reply(
        {
            "fields": [
                {"field": "area", "value": "100", "raw_text": "", "score": 1, "reason": "上下文似乎如此"}
            ]
        },
        page,
        (0, 0, 200, 200),
        cfg["models"]["primary"],
        "context_suggestion",
    )
    assert not result["area"]


def test_project_credentials_are_destination_bound(tmp_path, monkeypatch):
    from app import credentials

    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    (tmp_path / ".env").write_text(
        "AI_GUIJILIUDONG_SECRET='synthetic-only'\nUNRELATED_SECRET=must-not-read\n"
    )
    assert credentials.read_project_values() == {"AI_GUIJILIUDONG_SECRET": "synthetic-only"}
    assert credentials.credential("AI_GUIJILIUDONG_SECRET", "api.siliconflow.cn") == "synthetic-only"
    with pytest.raises(ValueError, match="域名"):
        credentials.credential("AI_GUIJILIUDONG_SECRET", "dashscope.aliyuncs.com")
    with pytest.raises(ValueError):
        credentials.credential("UNRELATED_SECRET", "api.siliconflow.cn")


def test_ocr_plain_text_does_not_invent_image_coordinates(cloud):
    from app.providers import CloudProvider

    page, cfg = cloud
    profile = cfg["models"]["primary"] | {"response_format": "ocr_text"}

    def reply(request):
        body = json.loads(request.content)
        assert len(body["messages"]) == 1 and body["messages"][0]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "model": "STUB",
                "choices": [{"message": {"content": "土地面积：１２３.４５平方米\n年租金：-120元"}}],
            },
        )

    result = CloudProvider(profile, httpx.MockTransport(reply)).identify(page)
    candidate = result["candidates"]["area"][0]
    assert candidate["raw_value"] == "１２３.４５平方米" and candidate["value"] == "123.45"
    assert candidate["bbox"] is None and candidate["source_locator"] is None
    assert candidate["score_meaning"] == "not_provided" and not candidate["can_publish"]
    assert result["candidates"]["annual"][0]["value"] == "-120"
    assert result["mode"] == "test_stub"


def test_qianfan_ocr_protocol_and_block_coordinates(cloud):
    from app.providers import CloudProvider

    page, cfg = cloud
    p = cfg["models"]["primary"] | {
        "protocol": "qianfan_ocr",
        "base_url": "https://qianfan.baidubce.com/v2/ocr/paddleocr",
        "model_id": "paddleocr-vl-0.9b",
    }

    def handler(request):
        body = json.loads(request.content)
        assert str(request.url) == p["base_url"]
        assert body["fileType"] == 1 and "messages" not in body
        assert not body["useDocUnwarping"] and not body["visualize"]
        return httpx.Response(
            200,
            json={
                "id": "stub",
                "result": {
                    "dataInfo": {"width": 100, "height": 100},
                    "layoutParsingResults": [
                        {
                            "prunedResult": {
                                "model_settings": {"use_doc_preprocessor": False},
                                "parsing_res_list": [
                                    {"block_content": "土地面积：10", "block_bbox": [10, 20, 90, 40]}
                                ],
                            }
                        }
                    ],
                },
            },
        )

    result = CloudProvider(p, httpx.MockTransport(handler)).identify(page)
    c = result["candidates"]["area"][0]
    assert c["bbox"] == [20, 40, 180, 80] and c["value"] == "10"
    assert c["score_meaning"] == "not_provided" and not c["can_publish"]
    assert result["mode"] == "test_stub" and result["returned_model"] is None


def test_multi_page_citations_normalization_and_no_automatic_publication(cloud, tmp_path):
    from app.cloud_cascade import run_document_cascade

    page, cfg = cloud
    second = page | {"id": "second-page"}
    calls = []

    def reply(request):
        body = json.loads(request.content)
        calls.append(body)
        fields = [
            {
                "field": "area",
                "value": "１２３.４５平方米",
                "raw_text": "面积１２３.４５平方米",
                "page_id": "test-page",
                "bbox": [0.1, 0.2, 0.4, 0.3],
            },
            {
                "field": "mode",
                "value": "随基准地价调整",
                "raw_text": "勾选随基准地价调整",
                "page_id": "second-page",
                "bbox": [0.1, 0.2, 0.4, 0.3],
            },
            {"field": "tenant", "value": "invalid", "raw_text": "invalid", "page_id": "not-in-input"},
        ]
        return httpx.Response(
            200,
            json={
                "model": "STUB",
                "choices": [{"message": {"content": json.dumps({"fields": fields, "warnings": []})}}],
            },
        )

    result = run_document_cascade(
        [page, second], tmp_path / "multi", config=cfg, transport=httpx.MockTransport(reply)
    )
    c = result["primary"]["candidates"]["area"][0]
    assert c["raw_value"] == "１２３.４５平方米" and c["value"] == "123.45"
    assert result["primary"]["candidates"]["mode"][0]["value"] == "随标准调整"
    assert not result["primary"]["candidates"]["tenant"]
    assert result["primary"]["rejected"] == [{"field": "tenant", "reason": "unknown_page_id"}]
    assert result["state"] == "needs_human" and not c["can_publish"]
    assert len([x for x in calls[0]["messages"][0]["content"] if x["type"] == "image_url"]) == 2
    count = len(calls)
    run_document_cascade([page, second], tmp_path / "multi", config=cfg, transport=httpx.MockTransport(reply))
    assert len(calls) == count


def test_ambiguous_numeric_candidate_is_not_silently_selected(cloud):
    page, cfg = cloud
    c = parse_reply(
        {"fields": [{"field": "area", "value": "100或200", "raw_text": "100或200"}]},
        page,
        (0, 0, 200, 200),
        cfg["models"]["primary"],
    )["area"][0]
    assert c["value"] == "100或200" and c["raw_value"] == "100或200"


def test_ocr_location_protocol_numbers_are_not_area_facts(cloud):
    from app.providers import parse_ocr_text

    page, cfg = cloud
    line = "土地面积：１２３.４５平方米<|LOC_120|><|LOC_700|>"
    candidates, diagnostics = parse_ocr_text(line + "\n" + line, page, cfg["models"]["primary"])
    assert [c["value"] for c in candidates["area"]] == ["123.45"]
    assert candidates["area"][0]["bbox"] is None
    assert diagnostics["location_markers_removed_from_extraction"] == 4
    assert diagnostics["duplicate_lines_excluded_from_extraction"] == 1
