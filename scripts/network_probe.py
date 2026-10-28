"""Bounded, credential-free HTTPS diagnostics on this machine; never changes network settings."""

import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import httpx

URLS = (
    "https://api.siliconflow.cn/v1/models",
    "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "https://qianfan.baidubce.com/v2/ocr/paddleocr",
    "https://help.aliyun.com/",
)


def probe(url, proxy):
    events = []

    def trace(name, info):
        event = {"event": name}
        if name == "http11.receive_response_headers.complete":
            value = info.get("return_value")
            if isinstance(value, tuple) and len(value) > 1:
                event["http_status"] = value[1]
        if name.endswith(".failed"):
            event["error_type"] = type(info.get("exception")).__name__
        events.append(event)

    started = time.monotonic()
    result = {"url": url, "credentials_sent": False, "proxy_used": bool(proxy)}
    try:
        with httpx.Client(
            proxy=proxy, trust_env=False, follow_redirects=False, timeout=httpx.Timeout(8, connect=5)
        ) as client:
            response = client.get(url, extensions={"trace": trace})
            result["target_http_status"] = response.status_code
    except Exception as error:
        result["error_type"] = type(error).__name__
    result.update(events=events, elapsed_seconds=round(time.monotonic() - started, 3))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["direct", "system-proxy"], required=True)
    args = parser.parse_args()
    proxy = None
    if args.mode == "system-proxy":
        proxy = urllib.request.getproxies().get("https")
        if not proxy:
            parser.error("No current HTTPS proxy is visible in this execution environment")
        parsed = urlsplit(proxy)
        if (
            parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or parsed.scheme != "http"
            or parsed.username
            or parsed.password
        ):
            parser.error(
                "Only an existing credential-free loopback HTTP proxy is supported; no proxy URL printed"
            )
    with ThreadPoolExecutor(max_workers=4) as pool:
        for result in pool.map(lambda url: probe(url, proxy), URLS):
            print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
