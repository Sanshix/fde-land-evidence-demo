import io
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from test_domain import fields

from app import store
from app.main import app

HEAD = {"X-FDE-Role": "reviewer", "X-FDE-Actor": "test-reviewer"}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("FDE_DISABLE_WORKER", "1")
    monkeypatch.setattr(store, "DATA", tmp_path)
    monkeypatch.setattr(store, "DB", tmp_path / "test.sqlite")
    with TestClient(app) as c:
        yield c


def new_doc(client):
    image = Image.new("RGB", (100, 100), "white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    response = client.post(
        "/api/import",
        files={"files": ("synthetic.png", buffer.getvalue(), "image/png")},
        data={"name": "SYNTHETIC-TEST-NOT-OCR"},
        headers=HEAD,
    )
    assert response.status_code == 200, response.text
    doc = client.get("/api/documents/" + response.json()["id"]).json()
    return doc, buffer.getvalue()


def reviewed(doc):
    f = fields()
    return {
        "version": doc["version"],
        "fields": f,
        "citations": {k: doc["pages"][0]["id"] for k in f},
        "payment_ids": [],
        "relief_ids": [],
        "reason": "Synthetic fixture validates the workflow only; not an AI accuracy result",
        "evidence_clear": True,
        "material_complete": True,
        "confirm_calculation": True,
        "confirm_payments": True,
    }


def preview(client, doc, body):
    r = client.post(f"/api/documents/{doc['id']}/preview", json=body, headers=HEAD)
    assert r.status_code == 200, r.text
    assert not r.json()["errors"], r.text
    return body | {"draft_hash": r.json()["draft_hash"]}


def test_confirmation_gate_and_unreadable_hold(client):
    doc, _ = new_doc(client)
    body = preview(client, doc, reviewed(doc))
    assert client.get("/api/ledger").json() == []
    assert (
        client.post(
            f"/api/documents/{doc['id']}/confirm", json=body | {"evidence_clear": False}, headers=HEAD
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/documents/{doc['id']}/confirm", json=body | {"unresolved_fields": ["area"]}, headers=HEAD
        ).status_code
        == 400
    )
    assert (
        client.post(
            f"/api/documents/{doc['id']}/hold",
            json={"version": doc["version"], "reason": "无法辨认面积，请补扫"},
            headers=HEAD,
        ).status_code
        == 200
    )
    new = client.get(f"/api/documents/{doc['id']}").json()
    assert (
        client.post(
            f"/api/documents/{doc['id']}/confirm", json=body | {"version": new["version"]}, headers=HEAD
        ).status_code
        == 409
    )
    assert client.get("/api/ledger").json() == []


def test_publish_repeat_withdraw_reimport_consistency(client):
    doc, raw = new_doc(client)
    body = preview(client, doc, reviewed(doc))
    r = client.post(f"/api/documents/{doc['id']}/confirm", json=body, headers=HEAD)
    assert r.status_code == 200, r.text
    initial = r.json()
    assert initial["payload"]["due"] == "120.00"
    assert client.post(f"/api/documents/{doc['id']}/confirm", json=body, headers=HEAD).status_code == 409
    duplicate = client.post(
        "/api/import",
        files={"files": ("renamed.png", raw, "image/png")},
        data={"name": "renamed"},
        headers=HEAD,
    ).json()
    assert duplicate["duplicate"]
    assert client.get("/api/ledger").json()[0]["digest"] == initial["digest"]
    current = client.get(f"/api/documents/{doc['id']}").json()
    r = client.post(
        f"/api/documents/{doc['id']}/withdraw",
        json={"version": current["version"], "reason": "撤回测试"},
        headers=HEAD,
    )
    assert r.status_code == 200, r.text
    assert r.json()["publication"]["payload"]["due"] == "0.00"
    duplicate = client.post(
        "/api/import", files={"files": ("again.png", raw, "image/png")}, data={"name": "again"}, headers=HEAD
    ).json()
    current = client.get(f"/api/documents/{doc['id']}").json()
    assert current["state"] == "needs_human"
    body = preview(client, current, reviewed(current))
    r = client.post(f"/api/documents/{doc['id']}/confirm", json=body, headers=HEAD)
    assert r.status_code == 200, r.text
    assert r.json()["digest"] == initial["digest"]
    assert r.json()["parcel_id"] == initial["parcel_id"]


def test_roles_origin_and_version_checks(client):
    doc, _ = new_doc(client)
    body = preview(client, doc, reviewed(doc))
    endpoint = f"/api/documents/{doc['id']}/confirm"
    assert client.post(endpoint, json=body).status_code == 403
    assert (
        client.post(endpoint, json=body, headers=HEAD | {"Origin": "https://evil.example"}).status_code == 403
    )
    assert client.post(endpoint, json=body | {"version": 99}, headers=HEAD).status_code == 409
    assert client.post(endpoint, json=body | {"citations": {}}, headers=HEAD).status_code == 400
    assert client.post(endpoint, json=body | {"draft_hash": "fake"}, headers=HEAD).status_code == 409


def test_batch_contributions_and_immutable_notice(client):
    doc, raw = new_doc(client)
    b = preview(client, doc, reviewed(doc))
    first = client.post(f"/api/documents/{doc['id']}/confirm", json=b, headers=HEAD).json()
    n = client.post(f"/api/notices/{first['id']}", json={}, headers=HEAD).json()
    assert n["status"] == "draft_not_sent"
    duplicate = client.post(
        "/api/import", files={"files": ("again.png", raw, "image/png")}, data={"name": "again"}, headers=HEAD
    ).json()
    current = client.get(f"/api/documents/{doc['id']}").json()
    r = client.post(
        f"/api/documents/{doc['id']}/withdraw",
        json={"version": current["version"], "reason": "仅撤第二批", "batch": duplicate["batch"]},
        headers=HEAD,
    )
    assert r.status_code == 200
    assert r.json()["remaining_contributions"] == 1
    assert client.get("/api/ledger").json()[0]["digest"] == first["digest"]
    with store.connection() as db:
        saved = json.loads(db.execute("SELECT payload FROM notices WHERE id=?", (n["id"],)).fetchone()[0])
    assert saved["arrears"] == "120.00"


def test_backup_and_source_fidelity(client):
    doc, _ = new_doc(client)
    r = client.post("/api/backup", json={}, headers=HEAD | {"X-FDE-Role": "admin"})
    assert r.status_code == 200
    from pathlib import Path

    folder = Path(r.json()["path"])
    db = sqlite3.connect(folder / "fde.sqlite")
    assert db.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    db.close()
    assert list((folder / "objects").glob("*.png"))
