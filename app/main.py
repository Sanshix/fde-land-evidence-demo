import csv
import hashlib
import io
import json
import os
import re
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import auth, governance, intake, rules, store
from .business import SAMPLES, calculate_documents, ensure_parcel, parcel_key, sources, suggest
from .cloud_cascade import run_cloud_cascade
from .domain import FIELDS, validate_fields
from .providers import public_status

ROOT = Path(__file__).resolve().parents[1]
SOURCE = None
STOP = threading.Event()


def actor(request, admin=False):
    origin = request.headers.get("origin")
    allowed_origins = set(
        os.environ.get("FDE_ALLOWED_ORIGINS", "http://127.0.0.1:8765,http://localhost:8765").split(",")
    )
    if origin and origin not in allowed_origins:
        raise HTTPException(403, "拒绝跨站写入")
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
    f = body.get("fields", {})
    errors = validate_fields(f)
    matches = suggest(f, SOURCE)
    if errors:
        return {"errors": errors, "matches": matches}
    doc["review"] = {
        "fields": f,
        "evidence_clear": True,
        "payment_ids": body.get("payment_ids", []),
        "relief_ids": body.get("relief_ids", []),
        "uncertainty": body.get("uncertainty", ""),
        "stop": body.get("stop", ""),
    }
    with store.connection() as db:
        key = parcel_key(f, SOURCE)
        parcel = db.execute("SELECT id FROM parcels WHERE business_key=?", (key,)).fetchone()
        current = (
            [
                store.doc_row(r)
                for r in db.execute(
                    "SELECT * FROM documents WHERE parcel_id=? AND state='published' AND id<>?",
                    (parcel[0], identifier),
                )
            ]
            if parcel
            else []
        )
        previous = store.latest(db, parcel[0]) if parcel else None
    try:
        calculation = calculate_documents(current + [doc], active_source())
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
    f = body.get("fields", {})
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
        if any(citations.get(k) not in valid_pages for k in f if f[k]):
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
        key = parcel_key(f, SOURCE)
        parcel_id = ensure_parcel(db, key)
        current = [
            store.doc_row(r)
            for r in db.execute(
                "SELECT * FROM documents WHERE parcel_id=? AND state='published' AND id<>?",
                (parcel_id, identifier),
            )
        ]
        try:
            calculation = calculate_documents(current + [doc], rules.source_for(db, SOURCE))
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        digest = review_hash(doc, body, calculation)
        if digest != body["draft_hash"]:
            raise HTTPException(409, "核算输入或同宗材料发生变化，请重新预览")
        db.execute(
            "UPDATE documents SET review=?,parcel_id=?,state='published',version=version+1,updated=? WHERE id=?",
            (store.dumps(doc["review"]), parcel_id, time.time(), identifier),
        )
        store.audit(
            db,
            who,
            "human_confirm",
            identifier,
            {"before": old, "after": doc["review"], "model_run_at": (doc["result"] or {}).get("started_at")},
        )
        publication = store.publish(db, parcel_id, calculation, who)
    return publication


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
                current = [
                    store.doc_row(r)
                    for r in db.execute(
                        "SELECT * FROM documents WHERE parcel_id=? AND state='published'", (doc["parcel_id"],)
                    )
                ]
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
def ledger():
    with store.connection() as db:
        return [
            store.latest(db, r[0]) for r in db.execute("SELECT id FROM parcels") if store.latest(db, r[0])
        ]


@app.post("/api/notices/{publication_id}")
def notice(publication_id: int, request: Request):
    who = actor(request)
    with store.connection() as db:
        row = db.execute("SELECT * FROM publications WHERE id=?", (publication_id,)).fetchone()
        if not row:
            raise HTTPException(404)
        payload = json.loads(row["payload"])
        if not payload.get("documents") or float(payload["arrears"]) <= 0:
            raise HTTPException(400, "当前快照无有效欠缴，不生成催缴通知")
        latest_doc = store.doc_row(
            db.execute("SELECT * FROM documents WHERE id=?", (payload["latest_document"],)).fetchone()
        )
        n = {
            "id": store.uid(),
            "status": "draft_not_sent",
            "publication": publication_id,
            "recipient": payload["recipient"],
            "as_of": payload["as_of"],
            "arrears": payload["arrears"],
            "annual": payload["annual"],
            "phone": payload["phone"],
            "payment_instructions": "缴款截止日、地点、渠道与主管部门联系方式尚未配置；不得发送正式通知。",
            "template": "exercise-notice-v2",
            "contract_fields": latest_doc["review"]["fields"],
        }
        from .outputs import notice_docx

        raw = notice_docx(n, payload)
        file_hash = hashlib.sha256(raw).hexdigest()
        objects = store.DATA / "objects"
        objects.mkdir(exist_ok=True)
        (objects / (file_hash + ".docx")).write_bytes(raw)
        n["docx_sha256"] = file_hash
        n["sms_draft"] = (
            f"【模拟催缴草稿】{payload['recipient']}：{payload['address']}截至{payload['as_of']}欠缴{payload['arrears']}元。缴款截止日、地点和联系人待主管部门确认。未发送。"
        )
        db.execute(
            "INSERT INTO notices(id,publication_id,payload,created) VALUES(?,?,?,?)",
            (n["id"], publication_id, store.dumps(n), time.time()),
        )
        store.audit(db, who, "notice_draft_not_sent", str(publication_id), {"notice": n["id"]})
    return n


@app.get("/api/export")
def export():
    documents = [d for d in store.list_docs() if d["state"] == "published"]
    ledgers = ledger()
    names = {d["id"]: d["name"] for d in documents}
    contract_rows = [
        {
            "档案号": d["name"],
            **{label: d["review"]["fields"].get(k, "") for k, label in FIELDS.items()},
            "处理方式": "人工复核",
        }
        for d in documents
    ]
    ledger_rows, issue_rows = [], []
    for pub in ledgers:
        p = pub["payload"]
        if not p["documents"]:
            continue
        key = names.get(p.get("latest_document"), p.get("latest_document"))
        ledger_rows.append(
            {
                "宗地键": key,
                "档案号列表": ";".join(names.get(i, i) for i in p["documents"]),
                "不动产单元号": p["registry_unit"],
                "累计应缴": p["due"],
                "累计实缴": p["paid"],
                "欠缴金额": p["arrears"],
                "2026年度应缴": next((x["due"] for x in p["annual"] if x["year"] == 2026), "0.00"),
                "催缴对象": p["recipient"],
                "处理方式": "人工复核",
            }
        )
        for issue in p["issues"]:
            issue_rows.append(
                {
                    "档案号": names.get(issue.get("document"), key),
                    "问题类型": issue["type"],
                    "说明": issue["reason"],
                }
            )
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w") as z:
        for name, rows, fallback in [
            ("合同要素.csv", contract_rows, ["档案号", *FIELDS.values(), "处理方式"]),
            (
                "宗地台账.csv",
                ledger_rows,
                [
                    "宗地键",
                    "档案号列表",
                    "不动产单元号",
                    "累计应缴",
                    "累计实缴",
                    "欠缴金额",
                    "2026年度应缴",
                    "催缴对象",
                    "处理方式",
                ],
            ),
            ("问题清单.csv", issue_rows, ["档案号", "问题类型", "说明"]),
        ]:
            buffer = io.StringIO()
            writer = csv.DictWriter(buffer, fieldnames=list(rows[0]) if rows else fallback)
            writer.writeheader()
            writer.writerows(rows)
            z.writestr(name, buffer.getvalue().encode("utf-8-sig"))
        z.writestr(
            "说明.txt", "本次只导出已人工确认的材料。字段与问题规则尚未覆盖完整比赛要求，不代表全量成绩。\n"
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

    destination = ROOT / "runs" / ("backup-" + store.uid())
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


@app.get("/api/tax-export")
def tax_export(year: int = 2026, quarter: int = 3):
    from .outputs import tax_workbook

    try:
        raw = tax_workbook(
            ledger(), [d for d in store.list_docs() if d["state"] == "published"], SOURCE, year, quarter
        )
        return Response(
            raw,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=tax-draft.xlsx"},
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


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
    doc = require_doc(identifier)
    check_version(doc, {"version": version})
    if doc["state"] in {"published", "withdrawn", "recognizing"}:
        raise HTTPException(409, "生效材料先撤回；识别执行中请等待，避免覆盖版本")
    if len(files) > 40:
        raise HTTPException(400, "补件文件数超过40")
    new_units = []
    new_manifests = []
    existing = {m["sha256"] for m in (doc.get("intake") or {}).get("files", [])}
    for f in files:
        raw = await f.read(25 * 1024 * 1024 + 1)
        try:
            units, manifest = intake.ingest(f.filename or "upload", raw, store.DATA / "objects")
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
        if db.execute("SELECT id FROM documents WHERE digest=? AND id<>?", (digest, identifier)).fetchone():
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
            },
        )
    return {"version": version + 1, "duplicate": False, "confirmation": "invalidated"}
