"""Explicit authorized live smoke on one fictional exercise page. No truth/scorer imports."""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.credentials import credential, read_project_values
from app.providers import image_payload

parser = argparse.ArgumentParser()
parser.add_argument("--platform", choices=["siliconflow", "aliyun", "baidu"], required=True)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
source = ROOT.parent / "练习集/01_合同扫描件/LX-017_1.jpg"
run = (
    ROOT
    / "runs"
    / ("cloud-live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + args.platform)
)
run.mkdir(parents=True)
encoded, _ = image_payload(source)
base = {
    "sample": "LX-017_1.jpg",
    "input_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
    "mode": "live-cloud",
    "scope": "fictional_exercise",
    "cost": None,
    "cost_note": "unknown; usage recorded when returned",
}
secrets = [v for v in read_project_values().values() if v]


def save(name, result):
    text = json.dumps(result, ensure_ascii=False, indent=2)
    for secret in secrets:
        text = text.replace(secret, "[REDACTED]")
    (run / (name + ".json")).write_text(text)
    summary = {
        k: v
        for k, v in result.items()
        if k
        in {
            "provider",
            "requested_model",
            "returned_model",
            "http_status",
            "status",
            "elapsed_seconds",
            "usage",
            "error_code",
            "credential_state",
        }
    }
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return result


def call(provider, host, base_url, model, key_name):
    started = time.monotonic()
    r = {**base, "provider": provider, "destination_host": host, "requested_model": model}
    body = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "请识别图片中的可见文字，保留涂改前后数字；不确定的内容标注[无法确定]，不要依据常识补写。",
                    },
                    {"type": "image_url", "image_url": {"url": encoded}},
                ],
            }
        ],
        "max_tokens": 3000,
        "temperature": 0,
        "stream": False,
    }
    try:
        with httpx.Client(timeout=45, follow_redirects=False, trust_env=False) as client:
            response = client.post(
                base_url + "/chat/completions",
                headers={"Authorization": "Bearer " + credential(key_name, host)},
                json=body,
            )
        r["http_status"] = response.status_code
        data = response.json()
        if response.status_code == 200:
            r.update(
                status="response_received_not_accuracy_pass",
                returned_model=data.get("model"),
                response_id=data.get("id"),
                usage=data.get("usage"),
                raw_response=data.get("choices"),
            )
        else:
            error = data.get("error", {})
            r.update(
                status="provider_error",
                error_code=error.get("code") if isinstance(error, dict) else type(error).__name__,
                error_type=error.get("type") if isinstance(error, dict) else None,
            )
            # Keep sanitized provider message locally for protocol debugging; never output it to chat.
            r["provider_message"] = error.get("message") if isinstance(error, dict) else data.get("message")
    except Exception as e:
        r.update(status="transport_or_configuration_error", error_code=type(e).__name__)
    r["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return save(provider + "-" + model.split("/")[-1], r)


def silicon():
    host = "api.siliconflow.cn"
    wanted = ["PaddlePaddle/PaddleOCR-VL-1.5", "Qwen/Qwen3-VL-8B-Instruct"]
    try:
        with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
            r = client.get(
                "https://api.siliconflow.cn/v1/models",
                headers={"Authorization": "Bearer " + credential("AI_GUIJILIUDONG_SECRET", host)},
            )
        if r.status_code != 200:
            return save(
                "siliconflow-inventory",
                {"provider": "siliconflow", "status": "inventory_failed", "http_status": r.status_code},
            )
        ids = {x["id"] for x in r.json().get("data", [])}
        save(
            "siliconflow-inventory",
            {
                "provider": "siliconflow",
                "status": "inventory_verified",
                "available_requested_models": [m for m in wanted if m in ids],
                "destination_host": host,
            },
        )
        return [
            call("siliconflow", host, "https://api.siliconflow.cn/v1", m, "AI_GUIJILIUDONG_SECRET")
            if m in ids
            else save(
                "unavailable-" + m.split("/")[-1],
                {"provider": "siliconflow", "status": "model_not_listed", "requested_model": m},
            )
            for m in wanted
        ]
    except Exception as e:
        return save(
            "siliconflow-inventory",
            {"provider": "siliconflow", "status": "inventory_failed", "error_code": type(e).__name__},
        )


def baidu():
    from app.baidu_ocr import BaiduOCRError, parse_document

    started = time.monotonic()
    profile = json.loads((ROOT / "config/providers.baidu.json").read_text())["models"]["primary"]
    r = {**base, "provider": profile["provider"], "requested_model": profile["model_id"],
         "destination_host": "aip.baidubce.com", "auth_method": "application_ak_sk_oauth"}
    try:
        result = parse_document(encoded, "LX-017_1.jpg", profile, timeout=90)
        r.update(status="response_received_not_accuracy_pass", http_status=200,
                 returned_model=None, response_id=result["response_id"], task_id=result["task_id"],
                 raw_response=result["document"], http_requests=result["http_requests"])
    except BaiduOCRError as error:
        r.update(status="provider_error", error_stage=error.stage, error_code=error.code,
                 task_id=error.task_id)
    except Exception as error:
        r.update(status="transport_or_configuration_error", error_code=type(error).__name__)
    r["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return save("baidu-paddleocr-vl-service", r)


if args.platform == "siliconflow":
    silicon()
elif args.platform == "aliyun":
    from urllib.parse import urlparse

    profile = json.loads((ROOT / "config/providers.aliyun.json").read_text())["models"]["primary"]
    call("aliyun", urlparse(profile["base_url"]).hostname, profile["base_url"],
         profile["model_id"], profile["api_key_env"])
elif args.platform == "baidu":
    baidu()
print(
    json.dumps({"run_directory": str(run), "cloud_accuracy": "not_yet_evaluated"}, ensure_ascii=False),
    flush=True,
)
