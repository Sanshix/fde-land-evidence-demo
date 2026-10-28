"""Controlled agent workflow rehearsal, NOT an actual staff time/accuracy measurement."""

# ruff: noqa: E402
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
run = Path(sys.argv[1]).resolve()
folder = run / ("controlled-acceptance-" + str(time.time_ns()))
data = folder / "data"
data.mkdir(parents=True)
os.environ["FDE_DATA_DIR"] = str(data)
os.environ["FDE_DISABLE_WORKER"] = "1"
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient

from app import store
from app.business import SAMPLES
from app.main import app
from app.maintenance import create_backup, restore_backup

head = {"X-FDE-Role": "reviewer", "X-FDE-Actor": "controlled-agent-test-not-staff"}
checks = []


def check(name, value, detail=None):
    checks.append({"name": name, "passed": bool(value), "detail": detail})
    if not value:
        (folder / "checks.json").write_text(json.dumps(checks, ensure_ascii=False, indent=2))
        raise AssertionError(name + ": " + str(detail))


with TestClient(app) as client:
    images = sorted((SAMPLES / "01_合同扫描件").glob("LX-007_*.jpg"))
    files = [("files", (p.name, p.read_bytes(), "image/jpeg")) for p in images]
    imported = client.post(
        "/api/import", files=files, data={"name": "验收副本 LX-007（既有真实云结果，非新调用）"}, headers=head
    )
    check("real_3_pages_imported", imported.status_code == 200, imported.status_code)
    identifier = imported.json()["id"]
    copied = data / "runs" / "LX-007"
    shutil.copytree(run / "recognition/LX-007", copied)
    result = json.loads((copied / "result.json").read_text())
    result.update(
        original_mode=result["mode"],
        mode="existing_evidence",
        evidence_origin=str(run / "recognition/LX-007"),
    )
    with store.connection() as db:
        db.execute(
            "UPDATE documents SET result=?,state='needs_human',version=version+1 WHERE id=?",
            (store.dumps(result), identifier),
        )
        db.execute("UPDATE jobs SET state='done',run_path=? WHERE document_id=?", (str(copied), identifier))
        store.audit(
            db,
            "controlled-agent-test",
            "replay_recorded_real_cloud_result",
            identifier,
            {"source_run": run.name, "model_call_now": False},
        )
    doc = client.get("/api/documents/" + identifier).json()
    check(
        "pending_workbench_includes_record",
        any(r["id"] == identifier for r in client.get("/api/governance").json()),
    )
    fields = {k: v[0]["value"] for k, v in result["primary"]["candidates"].items() if v}
    check(
        "visual_read_values_match",
        fields["area"] == "579.46"
        and fields["annual"] == "927.14"
        and fields["start"] == "2022-03-01"
        and fields["end"] == "2032-02-29",
    )
    first, signed, _ = doc["pages"]
    citations = {k: signed["id"] if k == "signed" else first["id"] for k in fields}
    reason = "受控代理验收，已逐页查看LX007三页；将模型误引宗地图的租期/租金等改引第1页，签订日引第2页。仅演练单份合同闭环，未核验该宗全部历史材料，不是经办员实测，不得对外发文。"
    body = {
        "version": doc["version"],
        "fields": fields,
        "citations": citations,
        "payment_ids": [],
        "relief_ids": [],
        "reason": reason,
        "evidence_clear": True,
        "material_complete": True,
        "confirm_calculation": True,
        "confirm_payments": True,
    }
    preview = client.post(f"/api/documents/{identifier}/preview", json=body, headers=head)
    check(
        "deterministic_preview",
        preview.status_code == 200 and not preview.json().get("errors"),
        preview.json().get("errors"),
    )
    body["draft_hash"] = preview.json()["draft_hash"]
    rejected = client.post(
        f"/api/documents/{identifier}/confirm", json=body | {"evidence_clear": False}, headers=head
    )
    check("unchecked_evidence_rejected", rejected.status_code == 400, rejected.status_code)
    pub = client.post(f"/api/documents/{identifier}/confirm", json=body, headers=head)
    check("corrected_citation_publish", pub.status_code == 200, pub.text[:300])
    pub = pub.json()
    check("single_contract_math", pub["payload"]["due"] == "4481.18", pub["payload"]["due"])
    notice = client.post(f"/api/notices/{pub['id']}", json={}, headers=head).json()
    check("notice_draft_only", notice.get("status") == "draft_not_sent")
    (folder / "controlled-notice.docx").write_bytes(
        client.get(f"/api/notices/{notice['id']}/document").content
    )
    (folder / "controlled-tax.xlsx").write_bytes(client.get("/api/tax-export").content)
    duplicate = client.post(
        "/api/import", files=files, data={"name": "duplicate-real-sample"}, headers=head
    ).json()
    check(
        "idempotent_import",
        duplicate.get("duplicate") and client.get("/api/ledger").json()[0]["digest"] == pub["digest"],
    )
    current = client.get("/api/documents/" + identifier).json()
    response = client.post(
        f"/api/documents/{identifier}/withdraw",
        json={"version": current["version"], "reason": "受控撤回重导演练"},
        headers=head,
    )
    check(
        "withdraw_recalculates",
        response.status_code == 200 and response.json()["publication"]["payload"]["due"] == "0.00",
    )
    client.post("/api/import", files=files, data={"name": "reimport-real-sample"}, headers=head)
    current = client.get("/api/documents/" + identifier).json()
    second = body | {"version": current["version"]}
    preview = client.post(f"/api/documents/{identifier}/preview", json=second, headers=head).json()
    second["draft_hash"] = preview["draft_hash"]
    after = client.post(f"/api/documents/{identifier}/confirm", json=second, headers=head).json()
    check(
        "reimport_business_hash_stable",
        after.get("digest") == pub["digest"] and after.get("parcel_id") == pub["parcel_id"],
    )
    # Keep a second real difficult item pending for browser inspection, never manufacture certainty.
    held_images = sorted((SAMPLES / "01_合同扫描件").glob("LX-017_*.jpg"))
    response = client.post(
        "/api/import",
        files=[("files", (p.name, p.read_bytes(), "image/jpeg")) for p in held_images],
        data={"name": "LX-017 待核：涂改与证据冲突"},
        headers=head,
    )
    held = response.json()["id"]
    target = data / "runs" / "LX-017"
    shutil.copytree(run / "recognition/LX-017", target)
    difficult = json.loads((target / "result.json").read_text())
    difficult.update(original_mode=difficult["mode"], mode="existing_evidence")
    with store.connection() as db:
        db.execute(
            "UPDATE documents SET result=?,state='needs_human' WHERE id=?", (store.dumps(difficult), held)
        )
        db.execute("UPDATE jobs SET state='done',run_path=? WHERE document_id=?", (str(target), held))
    held_doc = client.get("/api/documents/" + held).json()
    response = client.post(
        f"/api/documents/{held}/hold",
        json={
            "version": held_doc["version"],
            "reason": "原文面积有涂改，各模型数值/证据冲突，仍需人员或补件核验，不采纳预计值。",
        },
        headers=head,
    )
    check(
        "ambiguous_real_case_held", response.status_code == 200 and len(client.get("/api/ledger").json()) == 1
    )
    backup = folder / "backup"
    create_backup(data, backup, store.DB)
    recovered = restore_backup(backup, folder / "restored")
    check(
        "backup_restore_real_sources_and_calls",
        recovered["integrity"] == "ok" and recovered["counts"]["documents"] == 2,
        recovered,
    )
    (folder / "checks.json").write_text(
        json.dumps(
            {
                "kind": "controlled_agent_workflow_not_staff_measurement",
                "checks": checks,
                "confirmed_single_contract_id": identifier,
                "held_id": held,
                "data": str(data),
                "actual_staff_seconds": None,
                "full_parcel_accounting_verified": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    (run / "latest-acceptance.txt").write_text(str(folder))
    print(
        json.dumps(
            {
                "acceptance": str(folder),
                "passed": sum(x["passed"] for x in checks),
                "checks": len(checks),
                "data": str(data),
            },
            ensure_ascii=False,
        )
    )
