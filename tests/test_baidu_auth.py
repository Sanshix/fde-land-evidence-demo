"""Protocol regression tests: credentials must never cross authentication schemes."""

from urllib.parse import parse_qs

import httpx
import pytest

from app import credentials
from app.baidu_ocr import TASK_URL, TOKEN_URL, BaiduOCRError, parse_document


@pytest.fixture
def profile(monkeypatch):
    monkeypatch.setenv("AI_BAIDU_KEY", "application-ak")
    monkeypatch.setenv("AI_BAIDU_SECRET", "application-sk")
    return {
        "provider": "baidu_ai_ocr",
        "model_id": "paddleocr-vl-service",
        "protocol": "baidu_ocr_oauth",
        "base_url": TASK_URL,
        "api_key_env": "AI_BAIDU_KEY",
        "secret_key_env": "AI_BAIDU_SECRET",
    }


def test_existing_aliyun_variable_and_baidu_destination_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(credentials, "ROOT", tmp_path)
    monkeypatch.delenv("AI_ALIYUN_KEY", raising=False)
    (tmp_path / ".env").write_text("AI_ALIYUN_KEY=synthetic-ali-key\n")
    assert credentials.credential("AI_ALIYUN_KEY", "dashscope.aliyuncs.com") == "synthetic-ali-key"
    with pytest.raises(ValueError, match="域名"):
        credentials.credential("AI_BAIDU_KEY", "qianfan.baidubce.com")


def handler_for(calls, result_url="https://ocr-result.bj.bcebos.com/result.json"):
    def handler(request):
        calls.append(request)
        form = parse_qs(request.content.decode()) if request.method == "POST" else {}
        assert "authorization" not in request.headers
        if str(request.url) == TOKEN_URL:
            assert form == {
                "grant_type": ["client_credentials"],
                "client_id": ["application-ak"],
                "client_secret": ["application-sk"],
            }
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        if request.url.host == "aip.baidubce.com":
            assert request.url.params["access_token"] == "synthetic-token"
            assert "client_secret" not in form
            if request.url.path.endswith("/query"):
                assert form == {"task_id": ["test-task"]}
                return httpx.Response(
                    200, json={"result": {"status": "success", "parse_result_url": result_url}}
                )
            assert form == {"file_data": ["aW1hZ2U="], "file_name": ["page.jpg"]}
            return httpx.Response(200, json={"result": {"task_id": "test-task"}})
        assert "access_token" not in request.url.params
        return httpx.Response(200, json={"pages": [{"text": "土地面积：10"}]})

    return handler


def test_oauth_submit_query_and_credential_free_result(profile):
    calls = []
    result = parse_document(
        "data:image/jpeg;base64,aW1hZ2U=", "page.jpg", profile, httpx.MockTransport(handler_for(calls))
    )
    assert result["document"]["pages"][0]["text"] == "土地面积：10"
    assert result["http_requests"] == 4 and result["returned_model"] is None
    assert "synthetic-token" not in str(result)


def test_result_url_cannot_redirect_credentials_or_fetch_arbitrary_hosts(profile):
    calls = []
    with pytest.raises(BaiduOCRError, match="unapproved_result_host"):
        parse_document(
            "aW1hZ2U=",
            "page.jpg",
            profile,
            httpx.MockTransport(handler_for(calls, "https://example.com/steal")),
        )
    assert len(calls) == 3


def test_token_failure_never_submits_image_or_leaks_response(profile):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"error": "invalid_client", "error_description": "application-sk"})

    with pytest.raises(BaiduOCRError) as error:
        parse_document("aW1hZ2U=", "page.jpg", profile, httpx.MockTransport(handler))
    assert len(calls) == 1
    assert error.value.stage == "token" and "application-sk" not in str(error.value)


def test_adapter_keeps_ocr_candidates_unverified(profile, tmp_path):
    from PIL import Image

    from app.providers import CloudProvider

    image = tmp_path / "page.png"
    Image.new("RGB", (20, 20), "white").save(image)

    def handler(request):
        if str(request.url) == TOKEN_URL:
            return httpx.Response(200, json={"access_token": "synthetic-token"})
        if request.url.host == "aip.baidubce.com":
            if request.url.path.endswith("/query"):
                return httpx.Response(
                    200,
                    json={
                        "result": {
                            "status": "success",
                            "parse_result_url": "https://ocr.bj.bcebos.com/result.json",
                        }
                    },
                )
            return httpx.Response(200, json={"result": {"task_id": "test-task"}})
        return httpx.Response(200, json={"pages": [{"text": "土地面积：10"}]})

    result = CloudProvider(profile, httpx.MockTransport(handler)).identify({"id": "p", "path": str(image)})
    candidate = result["candidates"]["area"][0]
    assert candidate["value"] == "10" and candidate["bbox"] is None
    assert not candidate["verified"] and not candidate["can_publish"]
    assert result["mode"] == "test_stub"
