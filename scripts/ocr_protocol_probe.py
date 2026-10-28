"""One explicitly scoped protocol correction experiment; no answers or scoring feedback."""

# ruff: noqa: E402
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.providers import CloudProvider, approved_input, model_profile

config = json.loads((ROOT / "config/providers.siliconflow.json").read_text())
config["enabled"] = True
config["models"]["secondary"]["ocr_prompt"] = "OCR:"
page_path = ROOT.parent / "练习集/01_合同扫描件/LX-017_1.jpg"
page = {"id": "LX-017_1", "source": str(page_path), "path": str(page_path)}
approved_input(page, config)
profile = model_profile(config, "secondary")
folder = ROOT / "runs" / ("ocr-protocol-probe-" + str(time.time_ns()))
folder.mkdir()
base = {
    "status": "started",
    "input": page_path.name,
    "input_sha256": hashlib.sha256(page_path.read_bytes()).hexdigest(),
    "provider": profile["provider"],
    "model": profile["model_id"],
    "prompt": "OCR:",
    "scope": "fictional_exercise",
    "reference": "https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.5#paddleocr-vl-15-09b-usage-with-transformers",
    "monetary_cost": None,
}
(folder / "attempt.json").write_text(json.dumps(base, ensure_ascii=False, indent=2))
start = time.monotonic()
try:
    result = CloudProvider(profile).identify(page, timeout=75)
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    summary = base | {
        "status": "response_received_not_accuracy_pass",
        "elapsed_seconds": result["elapsed_seconds"],
        "usage": result.get("usage"),
        "finish_reason": result.get("finish_reason"),
        "diagnostics": result["raw_response"].get("extraction_diagnostics"),
    }
except Exception as error:
    summary = base | {
        "status": "failed",
        "error_type": type(error).__name__,
        "elapsed_seconds": round(time.monotonic() - start, 3),
    }
(folder / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(json.dumps({"folder": str(folder), **summary}, ensure_ascii=False))
