"""Local model worker. Writes only JSON payload to result file, never uploads input."""

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", str(ROOT / "models"))
os.environ.setdefault("PADDLE_PDX_MODEL_SOURCE", "BOS")
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("OMP_NUM_THREADS", "4")
from paddleocr import PaddleOCR

start = time.monotonic()
ocr = PaddleOCR(
    text_detection_model_name="PP-OCRv5_mobile_det",
    text_recognition_model_name="PP-OCRv5_server_rec",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
    device="cpu",
)
loaded = time.monotonic() - start
spec = json.loads(Path(sys.argv[1]).read_text())
results = []
for path in spec["paths"]:
    started = time.monotonic()
    try:
        prediction = next(iter(ocr.predict(path)))
        payload = prediction.json
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = payload.get("res", payload)
        lines = [
            {"text": t, "score": float(s), "box": list(map(float, b))}
            for t, s, b in zip(payload["rec_texts"], payload["rec_scores"], payload["rec_boxes"], strict=True)
        ]
        results.append(
            {
                "path": path,
                "lines": lines,
                "seconds": time.monotonic() - started,
                "model": "PP-OCRv5_mobile_det+server_rec",
                "mode": "live-local",
                "error": None,
            }
        )
    except Exception as e:
        results.append(
            {
                "path": path,
                "lines": [],
                "seconds": time.monotonic() - started,
                "model": "PP-OCRv5",
                "mode": "live-local",
                "error": str(e),
            }
        )
    Path(spec["output"]).write_text(
        json.dumps({"load_seconds": loaded, "pages": results}, ensure_ascii=False, indent=2)
    )
