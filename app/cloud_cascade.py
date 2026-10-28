import hashlib
import json
import time
from pathlib import Path

from .domain import FIELDS
from .governance import native_candidates
from .providers import (
    PROMPT_VERSION,
    CloudProvider,
    ProviderUnavailable,
    approved_input,
    configuration,
    model_profile,
    public_status,
)
from .recognition import assess, quality


def run_cloud_cascade(pages, run_dir, strategy="cascade", budget=180, config=None, transport=None):
    config = configuration() if config is None else config
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    out = {
        "mode": "test_stub" if transport else "live-cloud",
        "strategy": strategy,
        "prompt_version": PROMPT_VERSION,
        "started_at": time.time(),
        "candidates": {k: [] for k in FIELDS},
        "pages": [],
        "events": [],
        "external_calls": 0,
        "model_profiles": public_status(config),
        "third_model": "configured" if config.get("models", {}).get("third") else "not_configured",
        "context_model": "configured" if config.get("models", {}).get("context") else "not_configured",
        "cost": None,
        "cost_note": "调用费用未配置单价；以供应商账单核对，不将未知费用写0",
    }
    native = native_candidates(pages)
    out["candidates"] = native
    image_pages = [
        p for p in pages if p.get("path") and (p.get("kind") != "pdf_page" or p.get("needs_ocr", True))
    ]
    out["native_units"] = [
        {"id": p["id"], "lines": len(p.get("native_lines", [])), "status": "native_extracted"}
        for p in pages
        if p.get("native_lines")
    ]
    if not image_pages and pages:
        out.update(
            mode="native_only",
            state="needs_human",
            elapsed_seconds=round(time.monotonic() - started, 3),
            risks=assess(native),
        )
        (run_dir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        return out
    try:
        model_profile(config, "primary")
    except ProviderUnavailable as e:
        out.update(
            mode="unconfigured",
            state="needs_human",
            elapsed_seconds=0,
            risks=assess(native) + [{"field": None, "code": "provider_unavailable", "reason": str(e)}],
        )
        (run_dir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
        return out
    config_hash = hashlib.sha256(json.dumps(public_status(config), sort_keys=True).encode()).hexdigest()
    for page in image_pages:
        page_start = time.monotonic()
        candidates = {k: [] for k in FIELDS}
        q = quality(page["path"])
        result = {"id": page["id"], "quality": q, "stages": []}
        try:
            digest = approved_input(page, config)
            result["source_sha256"] = digest
        except ProviderUnavailable as e:
            result["error"] = str(e)
            out["pages"].append(result)
            out["events"].append({"stage": "egress_guard", "status": "blocked", "reason": str(e)})
            continue
        page_limit = min(float(config.get("page_budget_seconds", 60)), 90)
        for stage in ["primary"] if strategy == "primary" else ["primary", "secondary", "third", "context"]:
            remaining = min(
                budget - (time.monotonic() - started), page_limit - (time.monotonic() - page_start)
            )
            if remaining < 2:
                result["stages"].append({"stage": stage, "status": "budget_exhausted"})
                break
            risks = assess(candidates, [q])
            fields = list(
                dict.fromkeys(r["field"] for r in risks if r["field"] and r["code"] != "visual_review")
            )
            if stage != "primary" and not fields and not q["warnings"]:
                break
            try:
                profile = model_profile(config, stage)
            except ProviderUnavailable as e:
                result["stages"].append({"stage": stage, "status": "not_configured", "reason": str(e)})
                continue
            if stage == "secondary":
                first = config["models"]["primary"]
                if (first["provider"], first["model_id"]) == (profile["provider"], profile["model_id"]):
                    result["stages"].append(
                        {
                            "stage": stage,
                            "status": "skipped_same_model",
                            "reason": "同一模型重试不能算独立第二路径",
                        }
                    )
                    continue
            # Crop only when all unresolved fields have locations; missing fields require the page.
            boxes = [c["bbox"] for f in fields for c in candidates[f] if c.get("bbox")]
            missing = any(not candidates.get(f) for f in fields)
            region = None
            if stage != "primary" and boxes and not missing:
                region = [
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ]
            fingerprint = hashlib.sha256(
                f"{digest}|{page['id']}|{region}|{stage}|{config_hash}|{PROMPT_VERSION}".encode()
            ).hexdigest()
            checkpoint = run_dir / (fingerprint + ".json")
            try:
                if checkpoint.exists():
                    response = json.loads(checkpoint.read_text())
                    response["mode"] = "resumed_evidence"
                else:
                    out["external_calls"] += 1 if not transport else 0
                    response = CloudProvider(profile, transport).identify(
                        page,
                        region,
                        fields if stage != "primary" else None,
                        candidates if stage == "context" else None,
                        stage == "context",
                        timeout=min(40, remaining),
                    )
                    checkpoint.write_text(json.dumps(response, ensure_ascii=False, indent=2))
                result["stages"].append({"stage": stage, "status": "completed", **response})
                for k in FIELDS:
                    candidates[k].extend(response["candidates"][k])
            except Exception as e:
                # No automatic retry after ambiguous timeout: avoid unbounded calls/billing.
                result["stages"].append(
                    {
                        "stage": stage,
                        "status": "failed",
                        "reason": type(e).__name__ + ": " + str(e)[:250]
                        if isinstance(e, (ValueError, ProviderUnavailable))
                        else type(e).__name__,
                    }
                )
        for k in FIELDS:
            out["candidates"][k].extend(candidates[k])
        out["pages"].append(result)
        (run_dir / "checkpoint.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    out["risks"] = assess(out["candidates"], [p["quality"] for p in out["pages"]])
    out["elapsed_seconds"] = round(time.monotonic() - started, 3)
    out["state"] = "needs_human"
    out["budget_exhausted"] = time.monotonic() - started >= budget
    (run_dir / "result.json").write_text(json.dumps(out, ensure_ascii=False, indent=2))
    return out
