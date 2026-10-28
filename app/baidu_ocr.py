"""Baidu AI OCR application AK/SK flow, distinct from Qianfan V2 Bearer keys.

Official examples: https://ai.baidu.com/ai-doc/OCR/3mi73at9o
Tokens stay in memory; no credential-bearing URLs or exception bodies are logged.
"""

import time
from urllib.parse import urlparse

import httpx

from .credentials import credential

HOST = "aip.baidubce.com"
TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
TASK_URL = "https://aip.baidubce.com/rest/2.0/brain/online/v2/paddle-vl-parser/task"


class BaiduOCRError(ValueError):
    def __init__(self, stage, code, task_id=None):
        self.stage, self.code, self.task_id = stage, code, task_id
        super().__init__(f"百度OCR {stage}失败（{code}）")


def parse_document(encoded, filename, profile, transport=None, timeout=90, task_id=None):
    """Submit once, poll within a deadline. A timed-out task can be queried by ID."""
    if profile["base_url"] != TASK_URL:
        raise BaiduOCRError("configuration", "endpoint_mismatch")
    key = credential(profile["api_key_env"], HOST)
    secret = credential(profile["secret_key_env"], HOST)
    deadline = time.monotonic() + timeout
    calls = 0

    def request(client, method, url, stage, **kwargs):
        nonlocal calls
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BaiduOCRError(stage, "deadline_exceeded", task_id)
        calls += 1
        try:
            response = client.request(method, url, timeout=min(remaining, 30), **kwargs)
            if response.status_code != 200:
                raise BaiduOCRError(stage, f"HTTP_{response.status_code}", task_id)
            data = response.json()
        except (httpx.HTTPError, ValueError) as error:
            if isinstance(error, BaiduOCRError):
                raise
            raise BaiduOCRError(stage, type(error).__name__, task_id) from None
        if not isinstance(data, dict):
            raise BaiduOCRError(stage, "invalid_response", task_id)
        if data.get("error") or data.get("error_code"):
            raise BaiduOCRError(stage, data.get("error_code") or data["error"], task_id)
        return data

    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False) as client:
        auth = request(
            client,
            "POST",
            TOKEN_URL,
            "token",
            data={
                "grant_type": "client_credentials",
                "client_id": key,
                "client_secret": secret,
            },
        )
        token = auth.get("access_token")
        if not isinstance(token, str) or not token:
            raise BaiduOCRError("token", "missing_access_token")
        params = {"access_token": token}
        if not task_id:
            submitted = request(
                client,
                "POST",
                TASK_URL,
                "submit",
                params=params,
                data={
                    "file_data": encoded.split(",", 1)[-1],
                    "file_name": filename,
                },
            )
            task_id = (submitted.get("result") or {}).get("task_id")
            if not isinstance(task_id, str) or not task_id:
                raise BaiduOCRError("submit", "missing_task_id")
        while True:
            if time.monotonic() + 5 >= deadline:
                raise BaiduOCRError("query", "deadline_exceeded", task_id)
            time.sleep(0 if transport else 5)
            queried = request(
                client, "POST", TASK_URL + "/query", "query", params=params, data={"task_id": task_id}
            )
            result = queried.get("result") or {}
            status = result.get("status")
            if status == "success":
                break
            if status not in {"pending", "running"}:
                raise BaiduOCRError(
                    "query", "task_failed" if status == "failed" else "invalid_status", task_id
                )
        # Fetch only the documented BOS result, without passing API credentials.
        url = result.get("parse_result_url", "")
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".bcebos.com")
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise BaiduOCRError("result", "unapproved_result_host", task_id)
        document = request(client, "GET", url, "result")
        pages = document.get("pages")
        if not isinstance(pages, list) or len(pages) != 1 or not isinstance(pages[0].get("text"), str):
            raise BaiduOCRError("result", "invalid_single_page_result", task_id)
        return {
            "document": document,
            "task_id": task_id,
            "response_id": queried.get("log_id"),
            "http_requests": calls,
            "auth_method": "application_ak_sk_oauth",
            "service": "baidu_ai_ocr_paddle_vl_parser",
            "returned_model": None,
        }
