import hashlib
import json
import time
from pathlib import Path

from .cloud_attempts import AttemptRunner, atomic_json
from .domain import FIELDS
from .governance import native_candidates
from .providers import (
    PROMPT_VERSION,
    CloudProvider,
    ProviderUnavailable,
    approved_input,
    configuration,
    is_private_endpoint,
    model_profile,
    public_status,
    structured_vision,
)
from .recognition import assess, quality
from .recognition_workflow import WORKFLOW, PLAN2, PLAN3, escalation_codes, reported_uncertainty

ESCALATION_CODES = {"missing", "no_evidence", "single_path"}

def profile_signature(config):
    # Include the full endpoint path (region/deployment), never a credential value.
    profiles = {stage: {key: value for key, value in (profile or {}).items()
                        if key in {"provider", "model_id", "base_url", "protocol", "response_format", "ocr_prompt", "weight_revision", "enable_thinking", "thinking_budget", "max_tokens", "stream", "thinking_only", "full_transcription", "save_text_revision", "include_original_images"}}
                for stage, profile in config.get("models", {}).items()}
    return hashlib.sha256(json.dumps(profiles, sort_keys=True).encode()).hexdigest()


def finish_status(output, pages):
    primary = output.get("primary", {})
    whole_document = primary.get("status") == "completed" and not primary.get("rejected")
    completed, incomplete = [], []
    unknown = []
    for row in output.get("pages", []):
        if whole_document or any(s.get("status") == "completed" for s in row["stages"]):
            completed.append(row["id"])
        else:
            incomplete.append(row["id"])
        unknown.extend({"page_id": row["id"], "stage": s["stage"]} for s in row["stages"]
                       if s.get("outcome_unknown") or s.get("status") == "unknown")
    if primary.get("outcome_unknown"):
        unknown.append({"page_id": None, "stage": "primary"})
    incomplete += [p["id"] for p in pages if p["id"] not in completed and p["id"] not in incomplete]
    pending = output.get("fallback_pending", False)
    output["completion_status"] = ("partial" if pending else "completed" if not incomplete
                                   else "partial" if completed else "unknown" if unknown else "failed")
    output["page_coverage"] = {"completed": completed, "incomplete": incomplete, "total": len(pages)}
    output["unknown_attempts"] = unknown
    output["recoverable"] = bool(incomplete or pending or unknown)
    output["recovery_note"] = "完成表示读取协议结束，不代表字段准确、人工复核或发布完成"


def run_cloud_cascade(pages, run_dir, strategy="cascade", budget=float("inf"), config=None, transport=None,
                      defer_fallback=False, primary_result=None, retry_failed=False, retry_unknown=False):
    config = configuration() if config is None else config
    if config.get("workflow") in {PLAN2, PLAN3}:
        from .text_pipeline import run_text_pipeline
        return run_text_pipeline(pages, run_dir, strategy, budget, config, transport,
                                 defer_fallback, primary_result, retry_failed, retry_unknown)
    if strategy == "cascade" and not defer_fallback and primary_result is None and not config.get("document_context"):
        phase_start = time.monotonic()
        first = run_cloud_cascade(pages, run_dir, strategy, budget, config, transport,
                                  defer_fallback=True, retry_failed=retry_failed, retry_unknown=retry_unknown)
        if not first.get("fallback_pending"):
            return first
        return run_cloud_cascade(pages, run_dir, strategy, max(0, budget - (time.monotonic() - phase_start)),
                                  config, transport, primary_result=first,
                                  retry_failed=retry_failed, retry_unknown=retry_unknown)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    runner = AttemptRunner(run_dir, budget, transport=transport,
                           retry_failed=retry_failed, retry_unknown=retry_unknown)
    primary_deadline = started + budget
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
        "fourth_model": "configured" if config.get("models", {}).get("fourth") else "not_configured",
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
            completion_status="completed", recoverable=False,
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
    if config.get("document_context") and image_pages:
        return run_document_cascade(image_pages, run_dir, budget=budget, config=config, transport=transport,
                                    defer_fallback=defer_fallback, primary_result=primary_result, strategy=strategy,
                                    retry_failed=retry_failed, retry_unknown=retry_unknown,
                                    native=native, native_units=out["native_units"])
    config_hash = profile_signature(config)
    seeded = {p["id"]: p for p in (primary_result or {}).get("pages", [])}
    document_evidence = {k: list(rows) for k, rows in native.items()}
    for previous in seeded.values():
        for stage in previous.get("stages", []):
            if stage["stage"] == "primary" and stage["status"] == "completed":
                for key, rows in stage["candidates"].items():
                    document_evidence[key].extend(rows)
    out["fallback_pending"] = False
    out["external_calls"] = (primary_result or {}).get("external_calls", 0)
    for page in image_pages:
        page_start = time.monotonic()
        candidates = {k: [] for k in FIELDS}
        q = quality(page["path"])
        result = {"id": page["id"], "quality": q, "stages": []}
        try:
            digest = approved_input(page, config)
            egress_ok, egress_reason = True, None
        except ProviderUnavailable as e:
            # 未获外发授权：只允许政务网/本机模型；公网路径逐个拦截
            digest = hashlib.sha256(Path(page.get("source", page["path"])).read_bytes()).hexdigest()
            egress_ok, egress_reason = False, str(e)
        result["source_sha256"] = digest
        stages_allowed = [
            stage
            for stage in (["primary"] if strategy == "primary" else ["primary", "secondary", "third", "fourth", "context"])
            if (config.get("workflow") != WORKFLOW or stage in config.get("models", {}))
            and (egress_ok or is_private_endpoint(config.get("models", {}).get(stage) or {}))
        ]
        if not stages_allowed:
            result["error"] = egress_reason
            out["pages"].append(result)
            out["events"].append({"stage": "egress_guard", "status": "blocked", "reason": egress_reason})
            continue
        page_limit = budget
        prior = seeded.get(page["id"])
        prior_elapsed = prior.get("elapsed_seconds", 0) if prior else 0
        if prior:
            for row in prior["stages"]:
                if row["stage"] == "primary":
                    result["stages"].append(row)
                    if row["status"] == "completed":
                        for key in FIELDS:
                            candidates[key].extend(row["candidates"][key])
            stages_allowed = [s for s in stages_allowed if s != "primary"]
        needs_review = False
        for stage in stages_allowed:
            remaining = min(
                runner.remaining(), page_limit - (time.monotonic() - page_start)
            )
            if stage == "primary":
                remaining = min(remaining, primary_deadline - time.monotonic())
            if remaining < 2:
                result["stages"].append({"stage": stage, "status": "budget_exhausted"})
                break
            evidence = document_evidence if primary_result else candidates
            risks = assess(evidence, [q])
            fields = sorted({r["field"] for r in risks if r["field"] and r["code"] in escalation_codes(config)
                             and (not evidence.get(r["field"]) or any(c.get("source_page_id") == page["id"] for c in evidence[r["field"]]))})
            unread = not any(s.get("status") == "completed" for s in result["stages"])
            independently_read = any(s.get("status") == "completed" and s["stage"] != "primary" for s in result["stages"])
            if config.get("workflow") == WORKFLOW:
                if stage != "primary":
                    needs_review = needs_review or bool(fields or q["warnings"] or unread or reported_uncertainty(result["stages"]))
                    if not needs_review:
                        break
            elif stage != "primary" and not fields and (not q["warnings"] or independently_read) and not unread:
                break
            if defer_fallback and stage != "primary":
                out["fallback_pending"] = any(config.get("models", {}).get(s) for s in stages_allowed if s != "primary")
                break
            try:
                profile = model_profile(config, stage)
            except ProviderUnavailable as e:
                result["stages"].append({"stage": stage, "status": "not_configured", "reason": str(e)})
                continue
            if stage in {"secondary", "third", "fourth"}:
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
            if config.get("workflow") != WORKFLOW and stage != "primary" and boxes and not missing:
                region = [
                    min(b[0] for b in boxes),
                    min(b[1] for b in boxes),
                    max(b[2] for b in boxes),
                    max(b[3] for b in boxes),
                ]
            if stage == "context":
                fields = fields or sorted(k for k, rows in candidates.items() if rows)
            fingerprint = hashlib.sha256(
                f"{digest}|{page['id']}|{region}|{fields if stage == 'context' else None}|{stage}|{config_hash}|{PROMPT_VERSION}".encode()
            ).hexdigest()
            suggestions = candidates
            if stage == "context":
                if config.get("workflow") == WORKFLOW:
                    suggestions = {"field_candidates": candidates, "page_transcriptions": [
                        s["transcription"] for s in result["stages"]
                        if s.get("status") == "completed" and s.get("transcription")]}
                fingerprint = hashlib.sha256((fingerprint + json.dumps(suggestions, sort_keys=True)).encode()).hexdigest()
            try:
                approved_input(page, config, profile)
            except ProviderUnavailable as error:
                result["stages"].append({"stage": stage, "status": "failed", "error_type": "ProviderUnavailable", "reason": str(error)})
                continue
            response = runner.call(
                fingerprint, stage, profile,
                lambda timeout, p=profile, pg=page, r=region, f=fields, s=stage, cs=suggestions: CloudProvider(p, transport).identify(
                    pg, r, f if s == "context" else None, cs if s == "context" else None,
                    s == "context", timeout=timeout), limit=min(remaining, float(profile.get("request_timeout_seconds", 120))),
            )
            result["stages"].append(response)
            if response["status"] == "completed":
                for k in FIELDS:
                    candidates[k].extend(response["candidates"][k])
                    document_evidence[k].extend(response["candidates"][k])
        result["elapsed_seconds"] = prior_elapsed + time.monotonic() - page_start
        for k in FIELDS:
            out["candidates"][k].extend(candidates[k])
        out["pages"].append(result)
        atomic_json(run_dir / "checkpoint.json", out)
    out["risks"] = assess(out["candidates"], [p["quality"] for p in out["pages"]])
    out["elapsed_seconds"] = round(time.monotonic() - started + (primary_result or {}).get("elapsed_seconds", 0), 3)
    out["state"] = "needs_human"
    out["budget_exhausted"] = runner.remaining() <= 0
    out["external_calls"] += runner.external_calls
    finish_status(out, image_pages)
    atomic_json(run_dir / "result.json", out)
    return out


def run_document_cascade(pages, run_dir, budget=float("inf"), config=None, transport=None,
                         defer_fallback=False, primary_result=None, strategy="cascade",
                         retry_failed=False, retry_unknown=False, native=None, native_units=None):
    """Bounded multi-page read with a durable, sequential per-page recovery path."""
    config = configuration() if config is None else config
    folder = Path(run_dir)
    folder.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    runner = AttemptRunner(folder, budget, transport=transport,
                           retry_failed=retry_failed, retry_unknown=retry_unknown)
    primary = model_profile(config, "primary")
    # Check each actual destination again before its call, including private->public fallback.
    for page in pages:
        approved_input(page, config, primary)
    profile_hash = profile_signature(config)
    signature = hashlib.sha256(json.dumps({
        "pages": [(p["id"], hashlib.sha256(Path(p["path"]).read_bytes()).hexdigest()) for p in pages],
        "profiles": profile_hash, "prompt": PROMPT_VERSION,
    }, sort_keys=True).encode()).hexdigest()
    output = {
        "mode": "test_stub" if transport else "live-cloud", "strategy": "multi_page_then_independent_ocr",
        "prompt_version": PROMPT_VERSION, "signature": signature, "started_at": time.time(),
        "candidates": native or {k: [] for k in FIELDS}, "native_units": native_units or [],
        "pages": [], "events": [], "external_calls": (primary_result or {}).get("external_calls", 0),
        "model_profiles": public_status(config), "third_model": "configured" if config.get("models", {}).get("third") else "not_configured",
        "context_model": "multi_page_primary_not_a_third_model", "cost": None,
        "cost_note": "usage available; monetary cost unknown until official price/bill verified",
        "fallback_pending": False,
    }
    # On explicit recovery, preserve successful page work instead of resubmitting the entire document.
    previous = json.loads((folder / "result.json").read_text()) if (folder / "result.json").exists() else {}
    recovered_pages = previous.get("signature") == signature and any(
        s.get("status") == "completed" for p in previous.get("pages", []) for s in p.get("stages", []))
    if primary_result:
        response = primary_result["primary"]
    elif recovered_pages and previous.get("primary", {}).get("status") != "completed":
        response = previous["primary"]
    else:
        response = runner.call(signature + "-primary", "primary", primary,
            lambda timeout: CloudProvider(primary, transport).identify_document(pages, timeout=timeout),
            limit=min(120, float(config.get("primary_timeout_seconds", 120))))
    output["primary"] = response
    for key, rows in response.get("candidates", {}).items():
        output["candidates"][key].extend(rows)
    qualities = {p["id"]: quality(p["path"]) for p in pages}
    primary_risks = assess(output["candidates"], qualities.values())
    targets = set()
    if response.get("status") != "completed" or response.get("rejected"):
        targets.update(qualities)
    for risk in primary_risks:
        if risk["code"] not in ESCALATION_CODES:
            continue
        rows = response.get("candidates", {}).get(risk.get("field"), [])
        targets.update(c["source_page_id"] for c in rows) if rows else targets.update(qualities)
    for warning in response.get("warnings", []):
        identifier = warning.get("page_id") if isinstance(warning, dict) else None
        targets.update([identifier] if identifier in qualities else qualities)
    output["primary_risks"] = primary_risks
    output["escalation_pages"] = sorted(targets)
    stages = [s for s in ("secondary", "third", "fourth") if config.get("models", {}).get(s)]
    # A single-page retry of the structured primary has a different input shape. It is bounded
    # to once per page and never counts as an independent second model.
    if (response.get("status") != "completed" or response.get("rejected")) and structured_vision(primary):
        stages.append("recovery")
    output["fallback_pending"] = bool(defer_fallback and strategy != "primary" and targets and stages)
    for page in pages:
        row = {"id": page["id"], "quality": qualities[page["id"]],
               "primary_status": response.get("status", "failed"), "stages": []}
        if strategy == "primary" or page["id"] not in targets:
            row["stages"].append({"stage": "secondary", "status": "not_triggered"})
        elif defer_fallback:
            row["stages"].append({"stage": "secondary", "status": "deferred"})
        else:
            for stage in stages:
                if runner.remaining() < .05:
                    row["stages"].append({"stage": stage, "status": "budget_exhausted"})
                    break
                try:
                    profile = primary if stage == "recovery" else model_profile(config, stage)
                    approved_input(page, config, profile)
                    if stage != "recovery" and (profile["provider"], profile["model_id"]) == (primary["provider"], primary["model_id"]):
                        row["stages"].append({"stage": stage, "status": "skipped_same_model"})
                        continue
                except ProviderUnavailable as error:
                    row["stages"].append({"stage": stage, "status": "failed", "error_type": "ProviderUnavailable", "reason": str(error)})
                    continue
                digest = hashlib.sha256(Path(page["path"]).read_bytes()).hexdigest()
                key = hashlib.sha256(f"single-full|{digest}|{page['id']}|{stage}|{profile_hash}|{PROMPT_VERSION}".encode()).hexdigest()
                extra = runner.call(key, stage, profile,
                    lambda timeout, p=profile, pg=page: CloudProvider(p, transport).identify(pg, timeout=timeout),
                    limit=min(runner.remaining(), float(profile.get("request_timeout_seconds", 120))))
                row["stages"].append(extra)
                if extra["status"] == "completed":
                    for key, rows in extra["candidates"].items():
                        output["candidates"][key].extend(rows)
                # Stop only after evidence coverage and corroboration are available for this page.
                unresolved = [r for r in assess(output["candidates"], [qualities[page["id"]]])
                              if r["code"] in ESCALATION_CODES and r["field"]
                              and (not output["candidates"].get(r["field"]) or any(
                                  c.get("source_page_id") == page["id"] for c in output["candidates"][r["field"]]))]
                if not unresolved:
                    break
        output["pages"].append(row)
        atomic_json(folder / "checkpoint.json", output)
    output.update(risks=assess(output["candidates"], qualities.values()), state="needs_human",
        elapsed_seconds=round(time.monotonic() - started + (primary_result or {}).get("elapsed_seconds", 0), 3),
        budget_exhausted=runner.remaining() <= 0)
    output["external_calls"] += runner.external_calls
    finish_status(output, pages)
    atomic_json(folder / "result.json", output)
    return output
