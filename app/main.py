import hashlib
import io
import json
import os
import re
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import auth, governance, intake, operations, rules, store, triage
from .business import (
    SAMPLES,
    calculate_documents,
    completeness_hint,
    ensure_parcel,
    linked_parcels,
    normalize_fields,
    parcel_key,
    sources,
    suggest,
)
from .cloud_cascade import run_cloud_cascade
from .domain import DOCUMENT_ATTRIBUTES, FIELDS, validate_fields
from .providers import public_status

ROOT = Path(__file__).resolve().parents[1]
SOURCE = None
STOP = threading.Event()


def allowed_origins():
    port = os.environ.get("FDE_PORT", "8765")
    configured = {o.strip().rstrip("/") for o in os.environ.get("FDE_ALLOWED_ORIGINS", "").split(",") if o.strip()}
    return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"} | configured


def actor(request, admin=False):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in allowed_origins():
        raise HTTPException(403, "拒绝跨站写入；服务器部署请把访问地址加入 FDE_ALLOWED_ORIGINS")
    identity = getattr(request.state, "identity", None)
    role = identity["role"] if identity else request.headers.get("x-fde-role", "viewer")
    if role not in ({"admin"} if admin else {"reviewer", "admin"}):
        raise HTTPException(403, "当前演示角色无权执行此操作")
    name = identity["name"] if identity else unquote(request.headers.get("x-fde-actor", "")).strip()
    if not name or len(name) > 80:
        raise HTTPException(400, "需要填写经办人名称")
    return name


def require_doc(identifier):
    doc = store.get_doc(identifier)
    if not doc:
        raise HTTPException(404, "文书不存在")
    return doc


def check_version(doc, body):
    if body.get("version") != doc["version"]:
        raise HTTPException(409, "文书已被修改，请刷新再操作")


def queue(db, doc_id, strategy="cascade", fresh_budget=False):
    running = db.execute(
        "SELECT id FROM jobs WHERE document_id=? AND state IN ('queued','running')", (doc_id,)
    ).fetchone()
    if running:
        return running[0]
    origin = db.execute(
        "SELECT MIN(b.received) FROM batches b JOIN occurrences o ON b.id=o.batch WHERE o.document_id=? AND o.active=1",
        (doc_id,),
    ).fetchone()[0]
    job = store.uid()
    db.execute(
        "INSERT INTO jobs(id,document_id,state,strategy,created) VALUES(?,?,'queued',?,?)",
        (job, doc_id, strategy, time.time() if fresh_budget else origin or time.time()),
    )
    return job


def import_files(files, name, who, batch=None):
    if not files:
        raise ValueError("无可导入文件")
    batch = batch or store.uid()
    objects = store.DATA / "objects"
    objects.mkdir(exist_ok=True)
    pages, file_hashes, manifests = [], [], []
    for filename, raw in files:
        units, manifest = intake.ingest(filename, raw, objects)
        file_hashes.append(manifest["sha256"])
        pages.extend(units)
        manifests.append(manifest)
    if len(pages) > 200:
        raise ValueError("单份文书超过200个页/表/嵌入图单元，请拆分")
    status = (
        "failed"
        if all(m["technical_status"] == "failed" for m in manifests)
        else "partial"
        if any(m["technical_status"] != "parsed" for m in manifests)
        else "parsed"
    )
    inventory = {
        "files": manifests,
        "technical_status": status,
        "material_status": "pending_human_completeness",
        "business_status": "not_confirmed",
        "issues": [p for m in manifests for p in m["issues"]],
        "unit_count": len(pages),
    }
    digest = hashlib.sha256("|".join(sorted(file_hashes)).encode()).hexdigest()
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT OR IGNORE INTO batches(id,label,received,expected) VALUES(?,?,?,1)",
            (batch, name, time.time()),
        )
        existing = db.execute("SELECT * FROM documents WHERE digest=?", (digest,)).fetchone()
        if existing:
            identifier = existing["id"]
            db.execute(
                "INSERT OR IGNORE INTO occurrences(id,document_id,batch) VALUES(?,?,?)",
                (store.uid(), identifier, batch),
            )
            db.execute("UPDATE occurrences SET active=1 WHERE document_id=? AND batch=?", (identifier, batch))
            if existing["state"] == "withdrawn":
                db.execute(
                    "UPDATE documents SET state='needs_human',version=version+1,updated=? WHERE id=?",
                    (time.time(), identifier),
                )
                store.audit(
                    db,
                    who,
                    "reimport_cached_evidence",
                    identifier,
                    {"batch": batch, "notice": "复用既有真实OCR证据，非本次新识别；需再次确认"},
                )
            else:
                store.audit(db, who, "duplicate_import", identifier, {"batch": batch})
            return {"id": identifier, "duplicate": True, "batch": batch, "mode": "existing_evidence"}
        identifier = store.uid()
        db.execute(
            "INSERT INTO documents(id,digest,name,state,pages,created,updated,intake) VALUES(?,?,?,'queued',?,?,?,?)",
            (identifier, digest, name, store.dumps(pages), time.time(), time.time(), store.dumps(inventory)),
        )
        db.execute(
            "INSERT INTO occurrences(id,document_id,batch) VALUES(?,?,?)", (store.uid(), identifier, batch)
        )
        queue(db, identifier)
        store.audit(db, who, "import", identifier, {"batch": batch, "digest": digest, "pages": len(pages)})
        return {"id": identifier, "duplicate": False, "batch": batch}


def worker():
    while not STOP.wait(0.5):
        with store.connection() as db:
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if not row:
                continue
            job = dict(row)
            document = store.doc_row(
                db.execute("SELECT * FROM documents WHERE id=?", (job["document_id"],)).fetchone()
            )
            if not document or document["state"] in {"withdrawn", "published", "held"}:
                db.execute("UPDATE jobs SET state='cancelled' WHERE id=?", (job["id"],))
                continue
            if time.time() - job["created"] > 1200:
                db.execute(
                    "UPDATE jobs SET state='failed',error='已超过整包20分钟预算，转人工' WHERE id=?",
                    (job["id"],),
                )
                db.execute("UPDATE documents SET state='needs_human' WHERE id=?", (document["id"],))
                continue
            db.execute("UPDATE jobs SET state='running' WHERE id=?", (job["id"],))
            db.execute("UPDATE documents SET state='recognizing' WHERE id=?", (document["id"],))
        run_dir = store.DATA / "runs" / document["id"]
        try:
            result = run_cloud_cascade(
                document["pages"],
                run_dir,
                job["strategy"],
                budget=min(180, 1200 - (time.time() - job["created"])),
            )
            with store.connection() as db:
                # Do not overwrite a human decision made while recognition ran.
                current = db.execute(
                    "SELECT version,state FROM documents WHERE id=?", (document["id"],)
                ).fetchone()
                if current["version"] == document["version"] and current["state"] == "recognizing":
                    db.execute(
                        "UPDATE documents SET result=?,state='needs_human',version=version+1,updated=? WHERE id=?",
                        (store.dumps(result), time.time(), document["id"]),
                    )
                db.execute("UPDATE jobs SET state='done',run_path=? WHERE id=?", (str(run_dir), job["id"]))
                store.audit(
                    db,
                    "cloud-worker",
                    "recognition",
                    document["id"],
                    {
                        "run": job["id"],
                        "elapsed": result["elapsed_seconds"],
                        "mode": result["mode"],
                        "external_calls": result.get("external_calls", 0),
                        "risks": len(result["risks"]),
                    },
                )
        except Exception as e:
            with store.connection() as db:
                db.execute(
                    "UPDATE jobs SET state='failed',error=?,run_path=? WHERE id=?",
                    (str(e)[:1000], str(run_dir), job["id"]),
                )
                db.execute(
                    "UPDATE documents SET state='needs_human' WHERE id=? AND state='recognizing'",
                    (document["id"],),
                )
                store.audit(
                    db, "cloud-worker", "recognition_failed", document["id"], {"error": type(e).__name__}
                )


@asynccontextmanager
async def lifespan(app):
    global SOURCE
    store.init()
    SOURCE = sources()
    rules.initialize(SOURCE)
    STOP.clear()
    if not os.environ.get("FDE_DISABLE_WORKER"):
        threading.Thread(target=worker, daemon=True).start()
    yield
    STOP.set()


app = FastAPI(title="国有土地租赁 · 证据工作台", lifespan=lifespan)


@app.middleware("http")
async def authentication(request, call_next):
    request.state.received_at = time.time()
    if auth.users() is not None and request.url.path != "/healthz":
        identity = auth.verify(request.headers.get("authorization", ""))
        if identity is None:
            return Response(
                status_code=401, headers={"WWW-Authenticate": 'Basic realm="FDE local", charset="UTF-8"'}
            )
        request.state.identity = identity
    return await call_next(request)


@app.get("/healthz")
def health():
    with store.connection() as db:
        db.execute("SELECT 1").fetchone()
        version = db.execute("PRAGMA user_version").fetchone()[0]
    return {
        "status": "ok",
        "schema_version": version,
        "mode": "cloud-adapter",
        "worker_enabled": not bool(os.environ.get("FDE_DISABLE_WORKER")),
        "auth": "accounts" if auth.users() is not None else "loopback_demo",
    }


app.mount("/static", StaticFiles(directory=ROOT / "app/static"), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    return (ROOT / "app/static/index.html").read_text()


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/api/status")
def status():
    with store.connection() as db:
        jobs = [dict(r) for r in db.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT 30")]
    return {
        "batches": batch_status(),
        "fields": FIELDS,
        "documents": store.list_docs(),
        "jobs": jobs,
        "as_of": "2026-09-30",
        "models": public_status(),
        "scope": "本机演示身份；不是生产认证。核心闭环已实现，完整13类问题/全量评价仍待验证。",
    }


@app.get("/api/documents/{identifier}")
def document(identifier):
    doc = require_doc(identifier)
    with store.connection() as db:
        draft = db.execute("SELECT * FROM review_drafts WHERE document_id=?", (identifier,)).fetchone()
        doc["draft"] = json.loads(draft["payload"]) if draft and draft["version"] == doc["version"] else None
        doc["governance_issues"] = governance.issues_for(doc)
        candidates = (doc.get("result") or {}).get("candidates", {})
        first = {k: (v[0]["value"] if v else "") for k, v in candidates.items()}
        image_pages = len([p for p in doc["pages"] if p.get("path")])
        doc["hints"] = {
            "missing_pages": completeness_hint(
                (doc.get("review") or {}).get("fields", {}).get("doc_type") or first.get("doc_type") or "新签",
                image_pages,
                (doc.get("review") or {}).get("fields", {}).get("has_map") or first.get("has_map") or "是",
            ),
            "image_pages": image_pages,
        }
        doc["audit"] = [
            dict(r) for r in db.execute("SELECT * FROM audit WHERE target=? ORDER BY id DESC", (identifier,))
        ]
    return doc


@app.get("/api/pages/{identifier}/{page_id}")
def page(identifier, page_id):
    doc = require_doc(identifier)
    p = next((p for p in doc["pages"] if p["id"] == page_id), None)
    if not p:
        raise HTTPException(404)
    if not p.get("path"):
        raise HTTPException(400, "此单元为原生结构化证据，请查看段落/表/单元格或下载原件")
    return FileResponse(p["path"])


@app.post("/api/import")
async def upload(request: Request, files: list[UploadFile] = File(...), name: str = Form(...)):
    who = actor(request)
    if len(files) > 40:
        raise HTTPException(400, "单份文书最多40文件")
    payloads = []
    for f in files:
        raw = await f.read(25 * 1024 * 1024 + 1)
        payloads.append((f.filename or "upload", raw))
    try:
        return import_files(payloads, name[:100], who)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/sample/{name}")
def sample(name: str, request: Request):
    who = actor(request)
    if name not in {
        "LX-001",
        "LX-004",
        "LX-014",
        "LX-017",
        "LX-021",
        "LX-045",
        "LX-034",
        "LX-008",
        "LX-018",
        "LX-025",
    }:
        raise HTTPException(400, "快捷导入仅限声明过的开发集；留出集不在此入口")
    files = sorted((SAMPLES / "01_合同扫描件").glob(name + "_*.jpg"))
    return import_files([(f.name, f.read_bytes()) for f in files], name, who)


@app.post("/api/documents/{identifier}/retry")
async def retry(identifier, request: Request):
    who = actor(request)
    body = await request.json()
    doc = require_doc(identifier)
    check_version(doc, body)
    if doc["state"] in {"published", "withdrawn"}:
        raise HTTPException(409, "已发布/撤回材料不允许直接覆盖识别证据")
    with store.connection() as db:
        active = db.execute(
            "SELECT 1 FROM jobs WHERE document_id=? AND state IN ('queued','running')", (identifier,)
        ).fetchone()
    if active:
        # 否则版本号被抬高、运行中的任务结果被丢弃，文书会永久停在“等待处理”
        raise HTTPException(409, "识别任务尚在排队或执行，请等待完成后再重新识别")
    strategy = body.get("strategy", "cascade")
    if strategy not in {"cascade", "primary"}:
        raise HTTPException(400, "未知识别策略")
    with store.connection() as db:
        db.execute("UPDATE documents SET state='queued',version=version+1 WHERE id=?", (identifier,))
        job = queue(db, identifier, strategy, fresh_budget=True)
        store.audit(db, who, "retry", identifier, {"strategy": strategy, "job": job})
    return {"job": job}


@app.post("/api/documents/{identifier}/hold")
async def hold(identifier, request: Request):
    who = actor(request)
    body = await request.json()
    doc = require_doc(identifier)
    check_version(doc, body)
    if doc["state"] == "published":
        raise HTTPException(409, "已发布材料请先撤回再修订")
    if not body.get("reason"):
        raise HTTPException(400, "需说明不可辨认之处和补扫要求")
    with store.connection() as db:
        db.execute(
            "UPDATE documents SET state='held',version=version+1,updated=? WHERE id=?",
            (time.time(), identifier),
        )
        store.audit(db, who, "hold_for_rescan", identifier, {"reason": body["reason"][:1000]})
    return {"state": "held"}


@app.post("/api/documents/{identifier}/page")
async def adjust_page(identifier, request: Request):
    who = actor(request)
    body = await request.json()
    doc = require_doc(identifier)
    check_version(doc, body)
    if doc["state"] in {"published", "withdrawn", "recognizing"}:
        raise HTTPException(409, "请在未识别/未发布状态调整页面")
    pages = doc["pages"]
    index = next((i for i, p in enumerate(pages) if p["id"] == body.get("page_id")), None)
    if index is None:
        raise HTTPException(400, "未知页")
    if not pages[index].get("path"):
        raise HTTPException(400, "原生结构化单元不支持旋转或调页")
    if body.get("action") == "rotate":
        p = pages[index]
        dest = store.DATA / "objects" / f"{p['id']}-r{doc['version']}.png"
        with Image.open(p["path"]) as image:
            image.rotate(-90, expand=True).save(dest)
        p["path"] = str(dest)
        p["rotation"] = (p["rotation"] + 90) % 360
    elif body.get("action") == "previous" and index > 0:
        pages[index - 1], pages[index] = pages[index], pages[index - 1]
    else:
        raise HTTPException(400, "不支持的页面操作")
    with store.connection() as db:
        db.execute(
            "UPDATE documents SET pages=?,result=NULL,review=NULL,state='needs_human',version=version+1 WHERE id=?",
            (store.dumps(pages), identifier),
        )
        store.audit(db, who, "page_adjustment_invalidates_evidence", identifier, body)
    return {"state": "needs_human"}


@app.post("/api/documents/{identifier}/preview")
async def preview(identifier, request: Request):
    actor(request)
    body = await request.json()
    doc = require_doc(identifier)
    check_version(doc, body)
    try:
        f = normalize_fields(body.get("fields", {}))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    errors = validate_fields(f)
    matches = suggest(f, SOURCE)
    if errors:
        return {"errors": errors, "matches": matches}
    with store.connection() as db:
        target, others = parcel_context(db, f, identifier)
        siblings = parcel_documents(db, [target, *others] if target else [], identifier)
    matches = suggest(f, SOURCE, [d["review"]["fields"] for d in siblings])
    doc["review"] = {
        "fields": f,
        "evidence_clear": True,
        "payment_ids": body.get("payment_ids", []),
        "relief_ids": body.get("relief_ids", []),
        "uncertainty": body.get("uncertainty", ""),
        "stop": body.get("stop", ""),
    }
    with store.connection() as db:
        target, others = parcel_context(db, f, identifier)
        current = parcel_documents(db, [target, *others] if target else [], identifier)
        previous = store.latest(db, target) if target else None
    try:
        with store.connection() as db:
            source = rules.source_for(db, SOURCE)
            validate_payment_ownership(db, current + [doc], {target, *others}, source)
            calculation = calculate_documents(current + [doc], source)
    except ValueError as e:
        return {"errors": [str(e)], "matches": matches}
    return {
        "errors": [],
        "matches": matches,
        "calculation": calculation,
        "previous": previous,
        "draft_hash": review_hash(doc, body, calculation),
    }


@app.post("/api/documents/{identifier}/confirm")
async def confirm(identifier, request: Request):
    who = actor(request)
    body = await request.json()
    if (
        not body.get("evidence_clear")
        or not body.get("confirm_payments")
        or not body.get("reason", "").strip()
    ):
        raise HTTPException(400, "必须核对原件、关联缴费/减免并填写确认依据；不可辨认请挂起")
    if body.get("unresolved_fields"):
        raise HTTPException(400, "仍有不可辨认字段，禁止靠推理补事实")
    if not body.get("draft_hash"):
        raise HTTPException(400, "请先计算差异预览")
    try:
        f = normalize_fields(body.get("fields", {}))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    errors = validate_fields(f)
    if errors:
        raise HTTPException(400, "；".join(errors))
    citations = body.get("citations", {})
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        doc = store.doc_row(db.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone())
        if not doc:
            raise HTTPException(404)
        check_version(doc, body)
        if doc["state"] in {"held", "withdrawn", "published"}:
            raise HTTPException(409, "挂起请先重新识别；已发布先撤回；已撤回先重导")
        quality_errors = governance.validate_completeness(doc, body) + governance.validate_citations(
            doc, body
        )
        if quality_errors:
            raise HTTPException(400, "；".join(quality_errors))
        valid_pages = {p["id"] for p in doc["pages"]}
        if any(citations.get(k) not in valid_pages for k in f if f[k] and k not in DOCUMENT_ATTRIBUTES):
            raise HTTPException(400, "每个非空采纳字段必须关联原件页")
        old = doc["review"]
        doc["review"] = {
            "fields": f,
            "citations": citations,
            "citation_details": body.get("citation_details", {}),
            "material_complete": True,
            "confirm_calculation": True,
            "quality_resolutions": body.get("quality_resolutions", {}),
            "intake_version": (doc.get("intake") or {}).get("unit_count"),
            "evidence_clear": True,
            "payment_ids": body.get("payment_ids", []),
            "relief_ids": body.get("relief_ids", []),
            "reason": body["reason"][:2000],
            "uncertainty": body.get("uncertainty", ""),
            "stop": body.get("stop", ""),
            "actor": who,
            "at": time.time(),
            "review_elapsed_seconds": body.get("review_elapsed_seconds"),
            "method": "人工复核",
        }
        try:
            parcel_id, others, calculation = prepare_publication(db, doc, f, identifier)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        digest = review_hash(doc, body, calculation)
        if digest != body["draft_hash"]:
            raise HTTPException(409, "核算输入或同宗材料发生变化，请重新预览")
        publication = commit_publication(db, doc, old, parcel_id, others, calculation, who, "human_confirm")
    return publication


def selected_receipts(docs, source):
    wanted = {str(i) for d in docs for i in (d.get("review") or {}).get("payment_ids", [])}
    rows = {str(p["序号"]): p for p in source["payments"]}
    if wanted - rows.keys():
        raise ValueError("选中的缴费记录已不存在，请重新核对业务来源")
    return {("receipt", str(rows[i]["票据号"]).strip()) if rows[i].get("票据号")
            else ("row", i) for i in wanted}


def validate_payment_ownership(db, docs, parcel_ids, source):
    """A receipt may contribute to one active parcel; repeated same-parcel evidence is deduped by compute.

    Called under the publication's BEGIN IMMEDIATE transaction, including batch confirmation.
    Withdrawing all contributing documents releases the receipt without altering historical snapshots.
    """
    receipts = selected_receipts(docs, source)
    if not receipts:
        return
    identifiers = {d["id"] for d in docs}
    for row in db.execute("SELECT id,name,parcel_id,review FROM documents WHERE state='published'"):
        if row["id"] in identifiers or follow_parcel(db, row["parcel_id"]) in parcel_ids:
            continue
        overlap = receipts & selected_receipts([store.doc_row(row)], source)
        if overlap:
            labels = "、".join(key[1] for key in sorted(overlap))
            raise ValueError(f"票据 {labels} 已归入另一宗地（文书 {row['name']}），禁止重复抵扣；请先核对并撤回错误归集")


def prepare_publication(db, doc, fields, identifier, create=True):
    parcel_id, others = parcel_context(db, fields, identifier)
    current = parcel_documents(db, [parcel_id, *others], identifier)
    source = rules.source_for(db, SOURCE)
    validate_payment_ownership(db, current + [doc], {parcel_id, *others}, source)
    calculation = calculate_documents(current + [doc], source)
    if create:
        parcel_id, others = parcel_context(db, fields, identifier, create=True)
    return parcel_id, others, calculation


def commit_publication(db, doc, old, parcel_id, others, calculation, who, action):
    identifier = doc["id"]
    db.execute(
        "UPDATE documents SET review=?,parcel_id=?,state='published',version=version+1,updated=? WHERE id=?",
        (store.dumps(doc["review"]), parcel_id, time.time(), identifier),
    )
    store.audit(
        db,
        who,
        action,
        identifier,
        {"before": old, "after": doc["review"], "model_run_at": (doc["result"] or {}).get("started_at")},
    )
    for other in sorted(others):
        # 原合同编号把两组材料连成同一宗地：并入并保留历史快照
        db.execute("UPDATE documents SET parcel_id=? WHERE parcel_id=? AND state='published'", (parcel_id, other))
        db.execute("UPDATE parcels SET merged_into=? WHERE id=?", (parcel_id, other))
        store.publish(db, other, calculate_documents([], SOURCE), who)
        store.audit(db, who, "parcel_merge", other, {"into": parcel_id, "by_document": identifier})
    return store.publish(db, parcel_id, calculation, who)


@app.get("/api/batch-candidates")
def batch_candidates():
    rows = []
    with store.connection() as db:
        for row in db.execute("SELECT * FROM documents WHERE state='needs_human' ORDER BY created"):
            doc = store.doc_row(row)
            t = batch_candidate(db, doc)
            t.pop("review", None)
            rows.append({"id": doc["id"], "name": doc["name"], "version": doc["version"], **t})
    return rows


def batch_candidate(db, doc):
    t = triage.triage(doc)
    if not t["eligible"]:
        return t
    try:
        target, others = parcel_context(db, t["fields"], doc["id"])
        siblings = parcel_documents(db, [target, *others], doc["id"])
        matches = suggest(t["fields"], SOURCE, [d["review"]["fields"] for d in siblings])
        if matches.get("ambiguous") or any(p.get("match_strength") == "weak" for p in matches["payments"]):
            raise ValueError("缴费存在同名弱匹配或归宗歧义，须逐项人工核对，不可批量入账")
        review = {
            "fields": t["fields"], "citations": t["citations"], "citation_details": t["citation_details"],
            "evidence_clear": True, "material_complete": True, "confirm_calculation": True,
            "confirm_payments": True,
            "payment_ids": [str(p["序号"]) for p in matches["payments"] if p.get("preselect")],
            "relief_ids": [r["批准文号"] for r in matches["reliefs"]],
        }
        candidate = {**doc, "review": review}
        _, _, calculation = prepare_publication(db, candidate, t["fields"], doc["id"], create=False)
        if any(i["type"] == "存疑待确认" for i in calculation["issues"]):
            raise ValueError("存在存疑事项，转人工复核")
        t.update(review=review, calculation=calculation, matches=matches,
                 draft_hash=review_hash(candidate, review, calculation))
    except ValueError as error:
        t["eligible"] = False
        t["reasons"].append(str(error))
    return t


@app.post("/api/batch-confirm")
async def batch_confirm(request: Request):
    """经办人员对“有把握”的材料批量确认（处理方式=自动通过）。每份仍逐份核算、留痕，失败的转人工。"""
    who = actor(request)
    body = await request.json()
    if not body.get("reason", "").strip() or not body.get("reviewed_summary"):
        raise HTTPException(400, "批量确认须填写依据并声明已浏览汇总表")
    if not all(body.get(k) is True for k in
               ("evidence_clear", "material_complete", "confirm_calculation", "confirm_payments")):
        raise HTTPException(400, "批量确认仍须逐份核对原件、资料完整性、缴费和核算；不能仅凭模型一致发布")
    results = []
    for item in body.get("documents", [])[:200]:
        identifier = item.get("id")
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            doc = store.doc_row(db.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone())
            if not doc or doc["version"] != item.get("version"):
                results.append({"id": identifier, "published": False, "reason": "文书不存在或已变化"})
                continue
            t = batch_candidate(db, doc)
            if not t["eligible"]:
                results.append({"id": identifier, "published": False, "reason": "；".join(t["reasons"])})
                continue
            if not item.get("draft_hash") or item["draft_hash"] != t["draft_hash"]:
                results.append({"id": identifier, "published": False, "reason": "核算或同宗材料已变化，请刷新汇总表并重新核对"})
                continue
            old = doc["review"]
            doc["review"] = {
                **t["review"],
                "reason": body["reason"][:2000],
                "actor": who,
                "at": time.time(),
                "method": "批量确认",
            }
            quality_errors = governance.validate_completeness(doc, body) + governance.validate_citations(doc, t["review"])
            if quality_errors:
                results.append({"id": identifier, "published": False, "reason": "；".join(quality_errors)})
                continue
            try:
                parcel_id, others, calculation = prepare_publication(db, doc, t["fields"], identifier)
            except ValueError as e:
                results.append({"id": identifier, "published": False, "reason": str(e)})
                continue
            publication = commit_publication(db, doc, old, parcel_id, others, calculation, who, "batch_confirm")
            results.append({"id": identifier, "published": True, "publication": publication["id"]})
    return {"results": results, "published": sum(r["published"] for r in results)}


@app.post("/api/documents/{identifier}/withdraw")
async def withdraw(identifier, request: Request):
    who = actor(request)
    body = await request.json()
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        doc = store.doc_row(db.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone())
        if not doc:
            raise HTTPException(404)
        check_version(doc, body)
        if not body.get("reason"):
            raise HTTPException(400, "请填写撤回依据")
        if body.get("batch"):
            db.execute(
                "UPDATE occurrences SET active=0 WHERE document_id=? AND batch=?", (identifier, body["batch"])
            )
        else:
            db.execute("UPDATE occurrences SET active=0 WHERE document_id=?", (identifier,))
        remaining = db.execute(
            "SELECT COUNT(*) FROM occurrences WHERE document_id=? AND active=1", (identifier,)
        ).fetchone()[0]
        publication = None
        if not remaining:
            db.execute(
                "UPDATE documents SET state='withdrawn',version=version+1,updated=? WHERE id=?",
                (time.time(), identifier),
            )
            if doc["parcel_id"]:
                doc["parcel_id"] = follow_parcel(db, doc["parcel_id"])
                current = parcel_documents(db, [doc["parcel_id"]])
                try:
                    payload = calculate_documents(current, rules.source_for(db, SOURCE))
                except ValueError as e:
                    raise HTTPException(409, "撤回引出依赖冲突，保持旧快照：" + str(e)) from e
                publication = store.publish(db, doc["parcel_id"], payload, who)
        store.audit(
            db,
            who,
            "withdraw",
            identifier,
            {
                "reason": body["reason"],
                "remaining_source_contributions": remaining,
                "batch": body.get("batch", "ALL"),
            },
        )
    return {"remaining_contributions": remaining, "publication": publication}


@app.get("/api/ledger")
def ledger(street: str = "", status: str = "", arrears: str = "", keyword: str = ""):
    with store.connection() as db:
        rows = operations.entries(db)
    return operations.filtered(rows, street or None, status or None, arrears or None, keyword or None)


@app.get("/api/reminders")
def reminders(format: str = "json"):
    with store.connection() as db:
        items = operations.reminders(operations.entries(db))
    if format == "ics":
        return Response(
            operations.ics(items),
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=land-lease-reminders.ics"},
        )
    return items


@app.get("/api/stats")
def stats(format: str = "json"):
    with store.connection() as db:
        rows = operations.entries(db)
    result = operations.statistics(rows)
    if format == "csv":
        flat = [{"指标": k, "值": v} for k, v in result.items() if not isinstance(v, dict)]
        flat += [{"指标": f"合同状态·{k}", "值": v} for k, v in result["按合同状态"].items()]
        flat += [{"指标": f"街路·{k}·{kk}", "值": vv} for k, v in result["按街路"].items() for kk, vv in v.items()]
        return Response(
            operations.csv_bytes(flat, ["指标", "值"]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename=stats.csv"},
        )
    return result


@app.post("/api/notices/{publication_id}")
def notice(publication_id: int, request: Request):
    who = actor(request)
    from .outputs import notice_docx, notice_settings, sms_text

    with store.connection() as db:
        row = db.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        latest = store.latest(db, row["parcel_id"])
        if latest and latest["id"] != publication_id:
            raise HTTPException(409, "该台账版本已被新版本替代，请按最新版本生成")
        payload = json.loads(row["payload"])
        if not payload.get("documents") or float(payload["arrears"]) <= 0:
            raise HTTPException(400, "当前快照无有效欠缴，不生成催缴通知")
        latest_doc = store.doc_row(
            db.execute("SELECT * FROM documents WHERE id=?", (payload["latest_document"],)).fetchone()
        )
        settings = notice_settings()
        n = {
            "id": store.uid(),
            "status": "draft_not_sent",
            "publication": publication_id,
            "recipient": payload["recipient"],
            "as_of": payload["as_of"],
            "arrears": payload["arrears"],
            "annual": payload["annual"],
            "phone": payload["phone"],
            "phone_valid": bool(re.fullmatch(r"\d{11}", payload.get("phone") or "")),
            "settings_configured": settings["configured"],
            "template": "07_模板/国有土地租金催缴通知书（模板）",
            "contract_fields": latest_doc["review"]["fields"],
        }
        raw = notice_docx(n, payload, settings)
        file_hash = hashlib.sha256(raw).hexdigest()
        objects = store.DATA / "objects"
        objects.mkdir(exist_ok=True)
        (objects / (file_hash + ".docx")).write_bytes(raw)
        n["docx_sha256"] = file_hash
        n["sms"] = sms_text(payload, settings)
        n["sms_draft"] = n["sms"]["text"]
        db.execute(
            "INSERT INTO notices(id,publication_id,payload,created) VALUES(?,?,?,?)",
            (n["id"], publication_id, store.dumps(n), time.time()),
        )
        store.audit(db, who, "notice_draft_not_sent", str(publication_id), {"notice": n["id"]})
    return n


NOTICE_STATUSES = {"已审核", "已发送", "发送失败", "号码无效", "已缴清", "有异议", "作废"}


@app.post("/api/notices/{identifier}/events")
async def notice_event(identifier: str, request: Request):
    """催缴跟踪：短信通道待主管部门确认，发送结果由经办人员登记，形成可追溯的催缴记录。"""
    who = actor(request)
    body = await request.json()
    if body.get("status") not in NOTICE_STATUSES:
        raise HTTPException(400, "未知状态：" + "、".join(sorted(NOTICE_STATUSES)))
    with store.connection() as db:
        if not db.execute("SELECT 1 FROM notices WHERE id=?", (identifier,)).fetchone():
            raise HTTPException(404)
        db.execute(
            "INSERT INTO notice_events(notice_id,status,note,created,actor) VALUES(?,?,?,?,?)",
            (identifier, body["status"], str(body.get("note", ""))[:500], time.time(), who),
        )
        store.audit(db, who, "notice_event", identifier, {"status": body["status"]})
    return {"notice": identifier, "status": body["status"]}


@app.get("/api/notices")
def notice_list():
    with store.connection() as db:
        rows = []
        for r in db.execute("SELECT * FROM notices ORDER BY created DESC"):
            n = json.loads(r["payload"])
            events = [dict(e) for e in db.execute(
                "SELECT status,note,created,actor FROM notice_events WHERE notice_id=? ORDER BY id", (r["id"],)
            )]
            rows.append({"id": r["id"], "publication": r["publication_id"], "recipient": n["recipient"],
                         "arrears": n["arrears"], "phone": n.get("phone"), "created": r["created"],
                         "status": events[-1]["status"] if events else "草稿待审核",
                         "events": events, "sms": n.get("sms_draft")})
    return rows


@app.get("/api/export")
def export():
    documents = [d for d in store.list_docs() if d["state"] == "published"]
    names = {d["id"]: d["name"] for d in documents}
    methods = {d["id"]: d["review"].get("method") for d in documents}
    with store.connection() as db:
        ledgers = operations.entries(db)
    contract_rows = [operations.contract_row(d) for d in sorted(documents, key=lambda d: d["name"])]
    ledger_rows, issue_rows = [], []
    for pub in ledgers:
        p = pub["payload"]
        automatic = all(methods.get(i) == "批量确认" for i in p["documents"])
        ledger_rows.append(operations.ledger_row(pub, names, "自动通过" if automatic else "人工复核"))
        key = names.get(p.get("latest_document"), p.get("latest_document"))
        for issue in p["issues"]:
            issue_rows.append(
                {"档案号": names.get(issue.get("document"), key), "问题类型": issue["type"], "说明": issue["reason"]}
            )
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as z:
        z.writestr("合同要素.csv", operations.csv_bytes(contract_rows, operations.CONTRACT_COLUMNS))
        z.writestr("宗地台账.csv", operations.csv_bytes(ledger_rows, operations.LEDGER_COLUMNS))
        z.writestr("问题清单.csv", operations.csv_bytes(issue_rows, ["档案号", "问题类型", "说明"]))
        pending = [r for r in issue_rows if r["问题类型"] == "存疑待确认"]
        z.writestr("待确认清单.csv", operations.csv_bytes(pending, ["档案号", "问题类型", "说明"]))
        z.writestr(
            "说明.txt",
            "只导出已人工确认发布的材料。列名与练习集 08_标准答案 一致；“处理方式”按确认方式填写"
            "（批量确认=自动通过，逐项核对=人工复核）。待确认清单为存疑待确认事项。\n",
        )
    return Response(
        target.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=fde-exports.zip"},
    )


@app.post("/api/backup")
def backup(request: Request):
    who = actor(request, admin=True)
    from .maintenance import create_backup

    base = Path(os.environ.get("FDE_BACKUP_DIR", store.DATA.parent / (store.DATA.name + "-backups")))
    destination = base / ("backup-" + time.strftime("%Y%m%d-%H%M%S") + "-" + store.uid()[:6])
    manifest = create_backup(store.DATA, destination, store.DB)
    with store.connection() as db:
        store.audit(db, who, "backup", str(destination), {"files": len(manifest)})
    return {
        "path": str(destination),
        "files": len(manifest),
        "note": "数据库、原件与识别证据已备份；恢复至新目录后校验，不覆盖现有库。",
    }


def active_source():
    with store.connection() as db:
        return rules.source_for(db, SOURCE)


@app.get("/api/rules")
def read_rules():
    with store.connection() as db:
        active = rules.source_for(db, SOURCE)
        history = [
            dict(r) for r in db.execute("SELECT id,created,actor,reason FROM rulesets ORDER BY created DESC")
        ]
    return {
        "active": active["rate_version"],
        "rates": active["rates"],
        "history": history,
        "policy_version": "06-v1; rate-table changes only",
    }


def rate_impact(db, rates):
    current_source = rules.source_for(db, SOURCE)
    new_source = {**current_source, "rates": rates, "rate_version": rules.digest(rates)}
    impact = []
    for row in db.execute("SELECT id FROM parcels"):
        docs = [
            store.doc_row(r)
            for r in db.execute("SELECT * FROM documents WHERE parcel_id=? AND state='published'", (row[0],))
        ]
        if not docs:
            continue
        previous = store.latest(db, row[0])
        payload = calculate_documents(docs, new_source)
        impact.append({"parcel_id": row[0], "before": previous, "after": payload})
    result = {
        "previous_ruleset": current_source["rate_version"],
        "next_ruleset": new_source["rate_version"],
        "impact": impact,
    }
    return result, hashlib.sha256(store.dumps(result).encode()).hexdigest()


@app.post("/api/rules/preview")
async def preview_rules(request: Request):
    actor(request, admin=True)
    body = await request.json()
    try:
        rates = rules.validate(body.get("rates"))
        with store.connection() as db:
            result, fingerprint = rate_impact(db, rates)
        return {**result, "draft_hash": fingerprint}
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/rules/confirm")
async def confirm_rules(request: Request):
    who = actor(request, admin=True)
    body = await request.json()
    if not body.get("reason", "").strip():
        raise HTTPException(400, "必须说明新费率来源和变更依据")
    if not body.get("confirm_calculation"):
        raise HTTPException(400, "规则变化使旧核算确认失效，请核对逐宗差异并重新确认核算")
    try:
        rates = rules.validate(body.get("rates"))
        with store.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            result, fingerprint = rate_impact(db, rates)
            if fingerprint != body.get("draft_hash"):
                raise HTTPException(409, "规则或台账已变化，请重新预览差异")
            key = rules.digest(rates)
            db.execute(
                "INSERT OR IGNORE INTO rulesets VALUES(?,?,?,?,?)",
                (key, store.dumps(rates), time.time(), who, body["reason"][:1000]),
            )
            db.execute("UPDATE settings SET value=? WHERE key='active_ruleset'", (key,))
            for change in result["impact"]:
                store.publish(db, change["parcel_id"], change["after"], who)
            store.audit(
                db,
                who,
                "rate_change",
                key,
                {
                    "before": result["previous_ruleset"],
                    "reason": body["reason"],
                    "affected": len(result["impact"]),
                    "calculation_reconfirmed": True,
                    "preview_hash": fingerprint,
                },
            )
        return {"active": key, "affected": len(result["impact"])}
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/notices/{identifier}/document")
def download_notice(identifier):
    with store.connection() as db:
        row = db.execute("SELECT payload FROM notices WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise HTTPException(404)
    n = json.loads(row[0])
    return FileResponse(
        store.DATA / "objects" / (n["docx_sha256"] + ".docx"), filename="draft-notice-" + identifier + ".docx"
    )


@app.post("/api/tax-push")
async def tax_push(request: Request):
    """生成并留存一期税务推送单（表2第7项：留存每次推送的内容）。不对接税务系统。"""
    who = actor(request)
    body = await request.json()
    from .outputs import tax_workbook

    try:
        year, quarter = int(body.get("year", 2026)), int(body.get("quarter", 3))
        with store.connection() as db:
            ledgers = operations.entries(db)
        raw, count = tax_workbook(
            ledgers, [d for d in store.list_docs() if d["state"] == "published"], active_source(), year, quarter
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e)) from e
    digest = hashlib.sha256(raw).hexdigest()
    objects = store.DATA / "objects"
    objects.mkdir(exist_ok=True)
    (objects / (digest + ".xlsx")).write_bytes(raw)
    identifier = store.uid()
    with store.connection() as db:
        db.execute(
            "INSERT INTO tax_pushes(id,period,sha256,rows,created,actor) VALUES(?,?,?,?,?,?)",
            (identifier, f"{year}年第{quarter}季度", digest, count, time.time(), who),
        )
        store.audit(db, who, "tax_push_generated", identifier, {"period": f"{year}Q{quarter}", "rows": count})
    return {"id": identifier, "period": f"{year}年第{quarter}季度", "rows": count, "sha256": digest}


@app.get("/api/tax-pushes")
def tax_pushes():
    with store.connection() as db:
        return [dict(r) for r in db.execute("SELECT * FROM tax_pushes ORDER BY created DESC")]


@app.get("/api/tax-push/{identifier}/document")
def tax_push_document(identifier: str):
    with store.connection() as db:
        row = db.execute("SELECT * FROM tax_pushes WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise HTTPException(404)
    return FileResponse(
        store.DATA / "objects" / (row["sha256"] + ".xlsx"),
        filename=f"税务推送单-{row['period']}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/api/contracts/draft")
async def contract_draft(request: Request):
    """按制式模板生成新签/续期合同草稿并归档（表2第6项）。年租金由规则程序按现行计租标准计算。"""
    who = actor(request)
    body = await request.json()
    from .domain import GRADES, USES, cents, dec, normalize_mode, to_upper_money
    from .outputs import contract_docx

    kind = body.get("kind", "续期")
    if kind not in {"新签", "续期"}:
        raise HTTPException(400, "合同类型只能是新签或续期；变更请导入变更协议")
    with store.connection() as db:
        parcel_id = follow_parcel(db, body.get("parcel_id"))
        latest = store.latest(db, parcel_id) if parcel_id else None
        if not latest or not latest["payload"].get("documents"):
            raise HTTPException(404, "宗地没有已确认的台账")
        p = latest["payload"]
        base = store.doc_row(
            db.execute("SELECT * FROM documents WHERE id=?", (p["latest_document"],)).fetchone()
        )["review"]["fields"]
    f = {**base, **{k: v for k, v in (body.get("fields") or {}).items() if v not in (None, "")}}
    try:
        start = date.fromisoformat(body.get("start") or "")
        end = date.fromisoformat(body.get("end") or "")
    except ValueError as e:
        raise HTTPException(400, "请填写新合同起止日期") from e
    if end <= start:
        raise HTTPException(400, "终止日期须晚于起始日期")
    try:
        mode = normalize_mode(f.get("mode"))
        if mode not in {"固定", "随标准调整"}:
            raise ValueError("新合同计租方式须明确选择固定或随标准调整")
        source = active_source()
        overrides = body.get("fields") or {}
        grade = overrides.get("grade") or p.get("ledger", {}).get("土地级别") or f.get("grade")
        area_value = overrides.get("area")
        if area_value in (None, ""):
            area_value = p.get("ledger", {}).get("登记面积") or f.get("area")
        area = dec(area_value)
        if area <= 0 or grade not in GRADES or f.get("use") not in USES:
            raise ValueError("新合同须提供有效的面积、用途和土地级别")
        current = [r for r in source["rates"]
                   if r["用途"] == f["use"] and str(r["执行起始日"])[:10] <= start.isoformat()]
        if not current:
            raise ValueError("新合同起始日无适用计租标准，不能沿用旧租金生成草稿")
        rate = dec(max(current, key=lambda r: str(r["执行起始日"]))[grade])
        if rate < 0:
            raise ValueError("计租标准不能为负数")
        # 固定与浮动只决定新合同生效后的调整方式；初始年租金均按本次面积和生效标准计算。
        annual = cents(area * rate)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, str(e)) from e
    mode = "随基准地价调整" if mode == "随标准调整" else "固定"
    values = {
        "合同编号": body.get("contract_no") or "（编号待登记）",
        "原合同编号": base.get("contract_no", ""),
        "承租人": p["recipient"],
        "证件号码": f.get("identity", ""),
        "联系电话": p.get("phone") or f.get("phone", ""),
        "住所": f.get("residence", ""),
        "不动产单元号": p.get("registry_unit") or f.get("parcel_no", ""),
        "坐落": p.get("address") or f.get("address", ""),
        "土地面积": str(area),
        "用途": f.get("use", ""),
        "土地级别": grade or "",
        "东至": f.get("east", "　"), "南至": f.get("south", "　"), "西至": f.get("west", "　"), "北至": f.get("north", "　"),
        "起始日期": start.isoformat(),
        "终止日期": end.isoformat(),
        "年限": round(((end - start).days + 1) / 365.25),
        "年租金标准": str(rate),
        "计租标准版本": source.get("rate_version", "source-unversioned"),
        "年租金小写": str(annual),
        "年租金大写": to_upper_money(annual),
        "计租方式": mode,
        "签订日期": body.get("signed") or "（签订时填写）",
    }
    raw = contract_docx(values, kind, base.get("contract_no") if kind == "续期" else None)
    digest = hashlib.sha256(raw).hexdigest()
    (store.DATA / "objects").mkdir(exist_ok=True)
    (store.DATA / "objects" / (digest + ".docx")).write_bytes(raw)
    identifier = store.uid()
    with store.connection() as db:
        db.execute(
            "INSERT INTO generated_contracts(id,parcel_id,kind,payload,sha256,created,actor) VALUES(?,?,?,?,?,?,?)",
            (identifier, parcel_id, kind, store.dumps(values), digest, time.time(), who),
        )
        store.audit(db, who, "contract_draft", parcel_id, {"contract": identifier, "kind": kind})
    return {"id": identifier, "kind": kind, "values": values,
            "note": "合同草稿已归档；签订后扫描件仍须按导入流程入库确认"}


@app.get("/api/contracts/{identifier}/document")
def contract_document(identifier: str):
    with store.connection() as db:
        row = db.execute("SELECT * FROM generated_contracts WHERE id=?", (identifier,)).fetchone()
    if not row:
        raise HTTPException(404)
    return FileResponse(store.DATA / "objects" / (row["sha256"] + ".docx"), filename=f"{row['kind']}合同草稿-{identifier}.docx")


@app.post("/api/import-batch")
async def import_batch(request: Request, file: UploadFile = File(...)):
    who = actor(request)
    received = request.state.received_at
    raw = await file.read(150 * 1024 * 1024 + 1)
    if len(raw) > 150 * 1024 * 1024:
        raise HTTPException(400, "批次ZIP最多150MB")
    try:
        groups = {}
        total = 0
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if len(archive.infolist()) > 500:
                raise ValueError("ZIP文件条目超过500")
            for entry in archive.infolist():
                if (
                    entry.is_dir()
                    or entry.filename.startswith("__MACOSX/")
                    or Path(entry.filename).name == ".DS_Store"
                ):
                    continue
                if Path(entry.filename).suffix.lower() not in {
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".pdf",
                    ".docx",
                    ".xlsx",
                    ".doc",
                    ".xls",
                    ".docm",
                    ".xlsm",
                }:
                    raise ValueError("ZIP只允许图片/PDF/Word/Excel原件")
                total += entry.file_size
                if entry.file_size > 25 * 1024 * 1024 or total > 180 * 1024 * 1024:
                    raise ValueError("解压后文件超过限制")
                path = Path(entry.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("ZIP包含不安全路径")
                # Root files follow LX-017_1.jpg convention; folders are explicit document groups.
                name = (
                    path.parent.as_posix()
                    if path.parent.as_posix() != "."
                    else re.sub(r"_\d+$", "", path.stem)
                )
                groups.setdefault(name, []).append((path.name, archive.read(entry)))
        if not groups or len(groups) > 50:
            raise ValueError("一个批次需包含1到50份文书")
        for pages in groups.values():
            if len(pages) > 40:
                raise ValueError("单份文书页数超限，请检查分组")
        batch = store.uid()
        with store.connection() as db:
            db.execute(
                "INSERT INTO batches(id,label,received,expected) VALUES(?,?,?,?)",
                (batch, file.filename or "补档批次", received, len(groups)),
            )
            store.audit(db, who, "batch_received", batch, {"documents": len(groups), "bytes": len(raw)})
        results = []
        for name, pages in sorted(groups.items()):
            try:
                results.append(
                    import_files(
                        sorted(
                            pages,
                            key=lambda p: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", p[0])],
                        ),
                        name,
                        who,
                        batch,
                    )
                )
            except Exception as e:
                results.append({"name": name, "error": type(e).__name__ + ": " + str(e)[:200]})
        with store.connection() as db:
            db.execute(
                "UPDATE batches SET failures=? WHERE id=?", (sum("error" in r for r in results), batch)
            )
        return {
            "batch": batch,
            "received_at": received,
            "documents": results,
            "partial_failure": any("error" in r for r in results),
            "note": "部分失败保留已接收材料，可修正后重导；导入完成不等于整包业务验收完成",
        }
    except (ValueError, zipfile.BadZipFile) as e:
        raise HTTPException(400, str(e)) from e


def batch_status():
    out = []
    with store.connection() as db:
        for row in db.execute("SELECT * FROM batches ORDER BY received DESC LIMIT 30"):
            docs = [
                dict(r)
                for r in db.execute(
                    "SELECT d.id,d.state,d.updated FROM documents d JOIN occurrences o ON o.document_id=d.id WHERE o.batch=? AND o.active=1",
                    (row["id"],),
                )
            ]
            finished = (
                bool(docs)
                and not row["failures"]
                and len(docs) >= row["expected"]
                and all(d["state"] == "published" for d in docs)
            )
            end = max(d["updated"] for d in docs) if finished else time.time()
            out.append(
                {
                    **dict(row),
                    "documents": len(docs),
                    "published": sum(d["state"] == "published" for d in docs),
                    "held": sum(d["state"] == "held" for d in docs),
                    "elapsed_seconds": round(end - row["received"], 1),
                    "all_published": finished,
                    "within_20_minutes": finished and end - row["received"] <= 1200,
                    "acceptance": "计时指示，须另核对识别/归宗/金额与清单正确性",
                }
            )
    return out


@app.get("/api/original/{identifier}/{unit_id}")
def original_file(identifier, unit_id):
    doc = require_doc(identifier)
    page = next((p for p in doc["pages"] if p["id"] == unit_id), None)
    if not page:
        raise HTTPException(404)
    return FileResponse(page["source"], filename=Path(page["source"]).name)


@app.get("/api/governance")
def governance_queue():
    rows = []
    for doc in store.list_docs():
        if doc["state"] in {"published", "withdrawn"}:
            continue
        issues = governance.issues_for(doc)
        rows.append(
            {
                "id": doc["id"],
                "name": doc["name"],
                "version": doc["version"],
                "state": doc["state"],
                "technical_status": (doc.get("intake") or {}).get("technical_status", "legacy"),
                "material_status": "待核对完整性",
                "business_status": "未确认",
                "issues": issues,
                "categories": sorted({p["category"] for p in issues}),
                "candidate_count": sum(
                    len(cs) for cs in (doc.get("result") or {}).get("candidates", {}).values()
                ),
            }
        )
    return rows


@app.post("/api/documents/{identifier}/draft")
async def save_review_draft(identifier, request: Request):
    who = actor(request)
    body = await request.json()
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        doc = store.doc_row(db.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone())
        if not doc:
            raise HTTPException(404)
        check_version(doc, body)
        if doc["state"] in {"published", "withdrawn"}:
            raise HTTPException(409, "当前状态不可修改草稿")
        old = db.execute("SELECT payload FROM review_drafts WHERE document_id=?", (identifier,)).fetchone()
        db.execute(
            "INSERT INTO review_drafts VALUES(?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET version=excluded.version,payload=excluded.payload,actor=excluded.actor,at=excluded.at",
            (identifier, doc["version"], store.dumps(body), who, time.time()),
        )
        store.audit(
            db,
            who,
            "save_review_draft",
            identifier,
            {"before": json.loads(old[0]) if old else None, "after": body, "effective": False},
        )
    return {"saved": True, "effective": False, "version": doc["version"]}


def follow_parcel(db, parcel_id):
    seen = set()
    while parcel_id and parcel_id not in seen:
        seen.add(parcel_id)
        row = db.execute("SELECT merged_into FROM parcels WHERE id=?", (parcel_id,)).fetchone()
        if not row or not row[0]:
            return parcel_id
        parcel_id = row[0]
    return parcel_id


def parcel_context(db, fields, identifier, create=False):
    """Target parcel for a document: registry/cadastre key plus 原合同编号 links (续期、变更链)."""
    key = parcel_key(fields, SOURCE)
    row = db.execute("SELECT id FROM parcels WHERE business_key=?", (key,)).fetchone()
    direct = follow_parcel(db, row[0]) if row else None
    linked = {follow_parcel(db, p) for p in linked_parcels(db, fields, exclude=identifier)}
    candidates = ({direct} if direct else set()) | linked
    if not candidates:
        return (ensure_parcel(db, key) if create else None), set()

    def rank(pid):
        business_key = db.execute("SELECT business_key FROM parcels WHERE id=?", (pid,)).fetchone()[0]
        return (not re.fullmatch(r"\d{12}[A-Z]{2}\d{14}|\w{28}", business_key or ""), pid)

    target = min(candidates, key=rank)
    if create and not row:
        db.execute("INSERT INTO parcels(id,business_key,merged_into) VALUES(?,?,?)", (store.uid(), key, target))
    return target, candidates - {target}


def parcel_documents(db, parcel_ids, exclude=None):
    out = []
    for pid in dict.fromkeys(p for p in parcel_ids if p):
        out += [
            store.doc_row(r)
            for r in db.execute(
                "SELECT * FROM documents WHERE parcel_id=? AND state='published' AND id<>?", (pid, exclude or "")
            )
        ]
    return out


def review_hash(doc, body, calculation):
    included = {
        k: body.get(k)
        for k in (
            "fields",
            "citations",
            "citation_details",
            "payment_ids",
            "relief_ids",
            "stop",
            "uncertainty",
            "scenarios",
        )
    }
    binding = {
        "document": doc["id"],
        "version": doc["version"],
        "source_digest": doc["digest"],
        "input": included,
        "calculation": calculation,
    }
    return hashlib.sha256(store.dumps(binding).encode()).hexdigest()


@app.post("/api/documents/{identifier}/attach")
async def attach_material(
    identifier, request: Request, files: list[UploadFile] = File(...), version: int = Form(...)
):
    who = actor(request)
    if len(files) > 40:
        raise HTTPException(400, "补件文件数超过40")
    payloads = [(f.filename or "upload", await f.read(25 * 1024 * 1024 + 1)) for f in files]
    return attach_payloads(identifier, version, payloads, who)


def attach_payloads(identifier, version, payloads, who, extra_audit=None):
    doc = require_doc(identifier)
    check_version(doc, {"version": version})
    if doc["state"] in {"published", "withdrawn", "recognizing"}:
        raise HTTPException(409, "生效材料先撤回；识别执行中请等待，避免覆盖版本")
    new_units = []
    new_manifests = []
    existing = {m["sha256"] for m in (doc.get("intake") or {}).get("files", [])}
    for name, raw in payloads:
        try:
            units, manifest = intake.ingest(name, raw, store.DATA / "objects")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        if manifest["sha256"] not in existing:
            existing.add(manifest["sha256"])
            new_units.extend(units)
            new_manifests.append(manifest)
    if not new_units:
        return {"duplicate": True, "version": doc["version"]}
    if len(doc["pages"]) + len(new_units) > 200:
        raise HTTPException(400, "总证据单元数超过200")
    inventory = doc.get("intake") or {"files": [], "issues": []}
    inventory["files"].extend(new_manifests)
    inventory["issues"].extend(p for m in new_manifests for p in m["issues"])
    inventory["unit_count"] = len(doc["pages"]) + len(new_units)
    inventory["technical_status"] = (
        "partial" if any(m["technical_status"] != "parsed" for m in inventory["files"]) else "parsed"
    )
    digest = hashlib.sha256("|".join(sorted(existing)).encode()).hexdigest()
    with store.connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = store.doc_row(db.execute("SELECT * FROM documents WHERE id=?", (identifier,)).fetchone())
        check_version(current, {"version": version})
        if db.execute("SELECT id FROM documents WHERE digest=? AND id<>? AND state<>'withdrawn'", (digest, identifier)).fetchone():
            raise HTTPException(409, "该材料组合已存在，需人工处理重复文书")
        db.execute(
            "UPDATE documents SET pages=?,intake=?,digest=?,result=NULL,review=NULL,state='queued',version=version+1,updated=? WHERE id=?",
            (store.dumps(doc["pages"] + new_units), store.dumps(inventory), digest, time.time(), identifier),
        )
        queue(db, identifier, fresh_budget=True)
        store.audit(
            db,
            who,
            "attach_invalidates_confirmation",
            identifier,
            {
                "previous_version": version,
                "new_files": [m["sha256"] for m in new_manifests],
                "old_review_retained_in_audit": doc.get("review"),
                **(extra_audit or {}),
            },
        )
    return {"version": version + 1, "duplicate": False, "confirmation": "invalidated"}


@app.post("/api/documents/{identifier}/merge-into")
async def merge_into(identifier, request: Request):
    """补档材料命名/分组不整齐时，把误拆成单独文书的页并入目标文书（原件不改，旧文书撤回留痕）。"""
    who = actor(request)
    body = await request.json()
    source = require_doc(identifier)
    check_version(source, body)
    if source["state"] in {"published", "recognizing"}:
        raise HTTPException(409, "已发布请先撤回；识别执行中请等待")
    target = body.get("target")
    if not target or target == identifier:
        raise HTTPException(400, "请选择要并入的目标文书")
    payloads = [
        (m["name"], Path(m["source"]).read_bytes())
        for m in (source.get("intake") or {}).get("files", [])
        if m.get("source") and Path(m["source"]).is_file()
    ]
    if not payloads:
        raise HTTPException(400, "源文书没有可并入的原件")
    result = attach_payloads(target, body.get("target_version"), payloads, who, {"merged_from": identifier})
    with store.connection() as db:
        db.execute("UPDATE occurrences SET active=0 WHERE document_id=?", (identifier,))
        db.execute(
            "UPDATE documents SET state='withdrawn',version=version+1,updated=? WHERE id=?", (time.time(), identifier)
        )
        db.execute("UPDATE jobs SET state='cancelled' WHERE document_id=? AND state='queued'", (identifier,))
        store.audit(db, who, "merged_into", identifier, {"target": target, "reason": body.get("reason", "")})
    return {"target": target, **result}
