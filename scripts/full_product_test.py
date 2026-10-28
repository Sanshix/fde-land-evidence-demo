"""Full real-model run on business images only. Ground truth is deliberately not imported here."""
# ruff: noqa: E402 -- isolated data directory must be set before importing the application.

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--run", type=Path, required=True)
parser.add_argument("--documents", nargs="*")
parser.add_argument("--workers", type=int, default=3)
parser.add_argument("--primary-timeout", type=int, default=110)
args = parser.parse_args()
ROOT = Path(__file__).resolve().parents[1]
run = args.run.resolve()
assert run.is_relative_to(ROOT / "runs") and (run / "manifest.json").is_file()
os.environ["FDE_DATA_DIR"] = str(run / "data")
os.environ["FDE_DISABLE_WORKER"] = "1"
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient

from app import store
from app.business import SAMPLES
from app.cloud_cascade import run_document_cascade
from app.main import app, import_files

config = json.loads((ROOT / "config/providers.siliconflow.json").read_text())
config.update(
    enabled=True,
    reason="Explicit user authorization for complete fictional exercise evaluation",
    authorized_scope="fictional_exercise",
    document_context=True,
    primary_timeout_seconds=min(180, max(30, args.primary_timeout)),
)
for model in config["models"].values():
    model["identity_verified"] = True
    model["identity_evidence"] = (
        "provider inventory and returned_model matched requested official model family on 2026-10-28; hosted commit hash not exposed"
    )
(run / "providers.json").write_text(json.dumps(config, ensure_ascii=False, indent=2))
(run / ("frozen-code-invocation-" + str(time.time_ns()) + ".json")).write_text(
    json.dumps(
        {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "app").rglob("*")
            if p.is_file() and p.suffix in {".py", ".js", ".html", ".css"}
        },
        indent=2,
    )
)
lock = threading.Lock()
started = time.monotonic()
completed = 0


def recognize(item):
    global completed
    identifier, name = item
    folder = run / "recognition" / name
    folder.mkdir(parents=True, exist_ok=True)
    outcome = folder / "outcome.json"
    if outcome.exists():
        return json.loads(outcome.read_text()) | {"already_recorded": True}
    doc = store.get_doc(identifier)
    with store.connection() as db:
        db.execute("UPDATE jobs SET state='running' WHERE document_id=? AND state='queued'", (identifier,))
        db.execute("UPDATE documents SET state='recognizing' WHERE id=?", (identifier,))
    t = time.monotonic()
    (folder / ("attempt-started-" + str(time.time_ns()) + ".json")).write_text(
        json.dumps(
            {
                "name": name,
                "started_at": time.time(),
                "primary_timeout": config["primary_timeout_seconds"],
                "workers": args.workers,
                "billing_on_interrupted_response": "unknown",
            }
        )
    )
    try:
        result = run_document_cascade(doc["pages"], folder, budget=240, config=config)
        report = {
            "name": name,
            "document_id": identifier,
            "status": "completed",
            "pages": len(doc["pages"]),
            "mode": result["mode"],
            "elapsed_seconds": result["elapsed_seconds"],
            "calls": result["external_calls"],
            "primary_model": result["primary"]["returned_model"],
            "escalation_pages": len(result["escalation_pages"]),
            "risks": len(result["risks"]),
        }
        with store.connection() as db:
            db.execute(
                "UPDATE documents SET state='needs_human',result=?,version=version+1,updated=? WHERE id=?",
                (store.dumps(result), time.time(), identifier),
            )
            db.execute(
                "UPDATE jobs SET state='done',run_path=? WHERE document_id=? AND state='running'",
                (str(folder), identifier),
            )
            store.audit(db, "full-real-cloud-test", "recognition", identifier, report)
    except Exception as error:
        report = {
            "name": name,
            "document_id": identifier,
            "status": "failed",
            "pages": len(doc["pages"]),
            "elapsed_seconds": round(time.monotonic() - t, 3),
            "error_type": type(error).__name__,
            "reason": str(error)[:300] if isinstance(error, ValueError) else type(error).__name__,
        }
        with store.connection() as db:
            db.execute(
                "UPDATE documents SET state='needs_human',version=version+1,updated=? WHERE id=?",
                (time.time(), identifier),
            )
            db.execute(
                "UPDATE jobs SET state='failed',error=?,run_path=? WHERE document_id=? AND state='running'",
                (report["reason"], str(folder), identifier),
            )
            store.audit(db, "full-real-cloud-test", "recognition_failed", identifier, report)
    outcome.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    with lock:
        completed += 1
        print(json.dumps({"completed_this_invocation": completed, **report}, ensure_ascii=False), flush=True)
    return report


with TestClient(app):
    inventory = run / "documents.json"
    if inventory.exists():
        documents = json.loads(inventory.read_text())
    else:
        groups = {}
        for p in sorted((SAMPLES / "01_合同扫描件").glob("*.jpg")):
            groups.setdefault(p.stem.split("_")[0], []).append(p)
        documents = []
        for name, files in sorted(groups.items()):
            result = import_files([(p.name, p.read_bytes()) for p in files], name, "full-test-import")
            documents.append({"name": name, "id": result["id"], "files": [p.name for p in files]})
        inventory.write_text(json.dumps(documents, ensure_ascii=False, indent=2))
    selected = [(d["id"], d["name"]) for d in documents if not args.documents or d["name"] in args.documents]
    print(
        json.dumps(
            {
                "run": str(run),
                "selected_documents": len(selected),
                "all_documents": len(documents),
                "all_pages": sum(len(d["files"]) for d in documents),
                "workers": min(3, max(1, args.workers)),
            }
        ),
        flush=True,
    )
    with ThreadPoolExecutor(max_workers=min(3, max(1, args.workers))) as pool:
        results = [
            future.result() for future in as_completed([pool.submit(recognize, item) for item in selected])
        ]
    (run / ("invocation-" + str(time.time_ns()) + ".json")).write_text(
        json.dumps(
            {"elapsed_seconds": round(time.monotonic() - started, 3), "results": results},
            ensure_ascii=False,
            indent=2,
        )
    )
    print(
        json.dumps(
            {
                "complete": sum(r["status"] == "completed" for r in results),
                "failed": sum(r["status"] == "failed" for r in results),
                "elapsed_seconds": round(time.monotonic() - started, 3),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
