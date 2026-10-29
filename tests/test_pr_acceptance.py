"""Regression acceptance for PR #1. Synthetic evidence, no OCR accuracy claims or live calls."""

from decimal import Decimal

import pytest
from test_alignment import confirm, upload
from test_api import HEAD, preview, reviewed
from test_api import client as client_fixture
from test_domain import fields
from test_governance import make_docx

from app import governance, main, store
from app.domain import FIELDS, upper_money
from app.extraction import prose_candidates
from app.recognition import assess

client = client_fixture
ACK = {"reason": "Synthetic acceptance: reviewed source and calculations", "reviewed_summary": True,
       "evidence_clear": True, "material_complete": True, "confirm_calculation": True, "confirm_payments": True}


def candidate(client, name, color, values=None, candidate_changes=None, stored_risks=True):
    doc = upload(client, name, color)
    values = values or fields(doc_type="新签", contract_no=name, identity="synthetic-company")
    candidates = {key: [{"value": value, "model": model, "provider": "test-stub", "kind": "ocr",
                        "source_page_id": doc["pages"][0]["id"], "raw_text": value,
                        "bbox": [1, 1, 20, 20], "score": 0.99, **(candidate_changes or {})}
                       for model in ("model-a", "model-b")]
                  for key, value in values.items()}
    with store.connection() as db:
        db.execute("UPDATE documents SET state='needs_human',result=? WHERE id=?",
                   (store.dumps({"candidates": candidates, "risks": assess(candidates) if stored_risks else []}),
                    doc["id"]))
    return client.get("/api/documents/" + doc["id"]).json()


def batch_rows(client):
    return client.get("/api/batch-candidates").json()


def batch_publish(client, rows, ack=None):
    items = [{"id": r["id"], "version": r["version"], "draft_hash": r.get("draft_hash")} for r in rows]
    return client.post("/api/batch-confirm", json={**(ACK if ack is None else ack), "documents": items}, headers=HEAD)


def payments(monkeypatch, rows):
    monkeypatch.setattr(main, "SOURCE", {**main.SOURCE, "registry": [], "reliefs": [], "payments": rows})


def payment(serial=1, parcel=None):
    return {"序号": serial, "票据号": "receipt-100", "缴款人": "同一承租人", "宗地（收费员录入）": parcel,
            "金额（元）": 100, "所属年度": "2026", "缴费日期": "2026-06-01"}


def parcel(number):
    return fields(doc_type="新签", contract_no=f"contract-{number}", identity="synthetic-company",
                  tenant="同一承租人", parcel_no=f"9-9-99{number}", address=f"测试街{number}号")


def review_body(doc, values):
    return reviewed(doc) | {"fields": values, "citations": {k: doc["pages"][0]["id"] for k in values},
                            "payment_ids": ["1"]}


def test_name_only_payments_require_individual_review(client, monkeypatch):
    payments(monkeypatch, [payment()])
    candidate(client, "weak-1", "white", parcel(1))
    candidate(client, "weak-2", "black", parcel(2))
    suggestions = main.suggest(parcel(1), main.SOURCE)["payments"]
    assert len(suggestions) == 1 and suggestions[0]["match_strength"] == "weak"
    assert not suggestions[0]["preselect"]
    rows = batch_rows(client)
    assert all(not r["eligible"] and "弱匹配" in "；".join(r["reasons"]) for r in rows)
    assert batch_publish(client, rows).json()["published"] == 0
    assert client.get("/api/ledger").json() == []


def test_payment_claim_checked_again_at_commit_and_released_on_withdrawal(client, monkeypatch):
    payments(monkeypatch, [payment()])
    first = upload(client, "first", "white")
    second = upload(client, "second", "black")
    # Both previews are valid before either publication. Only one may consume the receipt.
    first_body = preview(client, first, review_body(first, parcel(1)))
    second_body = preview(client, second, review_body(second, parcel(2)))
    assert client.post(f"/api/documents/{first['id']}/confirm", json=first_body, headers=HEAD).status_code == 200
    rejected = client.post(f"/api/documents/{second['id']}/confirm", json=second_body, headers=HEAD)
    assert rejected.status_code == 400 and "禁止重复抵扣" in rejected.text
    assert len(client.get("/api/ledger").json()) == 1
    current = client.get(f"/api/documents/{first['id']}").json()
    from test_api import approved_withdraw
    assert approved_withdraw(client, first['id'],
                       json={"version": current["version"], "reason": "Correct wrong parcel assignment"},
                       headers=HEAD).status_code == 200
    second_body = preview(client, second, review_body(second, parcel(2)))
    assert client.post(f"/api/documents/{second['id']}/confirm", json=second_body, headers=HEAD).status_code == 200
    ledgers = client.get("/api/ledger").json()
    assert len(ledgers) == 1 and ledgers[0]["payload"]["paid"] == "100.00"


def test_same_receipt_across_source_rows_cannot_be_batch_double_counted(client, monkeypatch):
    payments(monkeypatch, [payment(1, "9-9-991"), payment(2, "9-9-992")])
    candidate(client, "strong-1", "white", parcel(1))
    candidate(client, "strong-2", "black", parcel(2))
    rows = batch_rows(client)
    assert all(r["eligible"] for r in rows)
    response = batch_publish(client, rows)
    assert response.status_code == 200
    assert response.json()["published"] == 1
    assert "禁止重复抵扣" in next(r["reason"] for r in response.json()["results"] if not r["published"])
    assert sum(Decimal(p["payload"]["paid"]) for p in client.get("/api/ledger").json()) == 100
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM parcels").fetchone()[0] == 1  # No orphan parcel on rejection.


def test_same_parcel_repeated_payment_is_deduped(client, monkeypatch):
    payments(monkeypatch, [payment(1, "9-9-991")])
    first = upload(client, "original", "white")
    confirm(client, first, parcel(1), payment_ids=["1"])
    change = upload(client, "change", "black")
    amended = {"doc_type": "变更", "contract_no": "change-1", "original_contract_no": "contract-1",
               "tenant": "同一承租人", "parcel_no": "9-9-991", "signed": "2026-05-01"}
    confirm(client, change, amended, payment_ids=["1"])
    ledger = client.get("/api/ledger").json()
    assert len(ledger) == 1 and ledger[0]["payload"]["paid"] == "100.00"


@pytest.mark.parametrize("change", [
    {"bbox": None}, {"raw_text": ""}, {"source_page_id": "outside-this-document"},
    {"bbox": [10, 10, 5, 5]}, {"score": 0.5}, {"kind": "context_suggestion"},
    {"kind": "native_extract", "source_locator": "invented-cell"},
])
@pytest.mark.parametrize("stored_risks", [True, False])
def test_batch_rechecks_source_evidence_and_risks(client, change, stored_risks):
    candidate(client, "bad-evidence", "white", candidate_changes=change, stored_risks=stored_risks)
    rows = batch_rows(client)
    assert not rows[0]["eligible"]
    assert batch_publish(client, rows).json()["published"] == 0
    assert client.get("/api/ledger").json() == []


def test_good_evidence_batch_requires_review_and_current_preview(client, monkeypatch):
    candidate(client, "good-evidence", "white")
    rows = batch_rows(client)
    assert rows[0]["eligible"]
    for key in ("evidence_clear", "material_complete", "confirm_calculation", "confirm_payments"):
        assert batch_publish(client, rows, ACK | {key: False}).status_code == 400
    assert batch_publish(client, [{**rows[0], "draft_hash": "outdated"}]).json()["published"] == 0
    # Source changes while the summary is open: even the same document/version must be reviewed again.
    monkeypatch.setattr(main, "SOURCE", {**main.SOURCE, "payments": [
        payment(1, "9-9-999") | {"缴款人": "模拟测试人"}]})
    assert batch_publish(client, rows).json()["published"] == 0
    refreshed = batch_rows(client)
    assert refreshed[0]["eligible"]
    assert batch_publish(client, refreshed).json()["published"] == 1
    published = client.get("/api/ledger").json()[0]["payload"]
    assert published["paid"] == "100.00"


def test_native_batch_persists_exact_source_locators(client):
    values = fields(doc_type="新签", contract_no="native", identity="synthetic-company")
    raw = make_docx([f"{FIELDS[key]}：{value}" for key, value in values.items()])
    result = client.post("/api/import", files={"files": ("native.docx", raw)}, data={"name": "native"}, headers=HEAD)
    doc = client.get("/api/documents/" + result.json()["id"]).json()
    candidates = governance.native_candidates(doc["pages"])
    with store.connection() as db:
        db.execute("UPDATE documents SET state='needs_human',result=? WHERE id=?",
                   (store.dumps({"candidates": candidates, "risks": assess(candidates)}), doc["id"]))
    rows = batch_rows(client)
    assert rows[0]["eligible"], rows[0]["reasons"]
    assert batch_publish(client, rows).json()["published"] == 1
    accepted = client.get("/api/documents/" + doc["id"]).json()
    assert accepted["review"]["citation_details"]["area"]
    assert governance.validate_citations(accepted, accepted["review"]) == []


@pytest.mark.parametrize("kind", ["新签", "续期"])
@pytest.mark.parametrize("mode", ["固定", "随标准调整"])
def test_new_contract_rent_uses_one_consistent_area_and_rate(client, kind, mode):
    doc = upload(client, "draft-rent", "white")
    pub = confirm(client, doc, fields(doc_type="新签", annual="120", area="10"))
    response = client.post("/api/contracts/draft", json={"parcel_id": pub["parcel_id"], "kind": kind,
        "start": "2027-01-01", "end": "2036-12-31", "fields": {"area": "20", "mode": mode}}, headers=HEAD)
    assert response.status_code == 200, response.text
    values = response.json()["values"]
    assert Decimal(values["土地面积"]) == 20
    assert Decimal(values["年租金小写"]) == Decimal(values["土地面积"]) * Decimal(values["年租金标准"]) == 48
    assert upper_money(values["年租金大写"]) == 48
    assert values["计租标准版本"] != "source-unversioned"


@pytest.mark.parametrize("overrides", [{"mode": "未勾选"}, {"grade": "五级"}, {"area": "-10"}, {"area": "NaN"}, {"area": 0}])
def test_contract_draft_rejects_unresolved_or_invalid_inputs(client, overrides):
    doc = upload(client, "invalid-draft", "white")
    pub = confirm(client, doc, fields(doc_type="新签"))
    response = client.post("/api/contracts/draft", json={"parcel_id": pub["parcel_id"], "kind": "续期",
        "start": "2027-01-01", "end": "2036-12-31", "fields": overrides}, headers=HEAD)
    assert response.status_code == 400
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM generated_contracts").fetchone()[0] == 0


@pytest.mark.parametrize("text,expected", [
    ("计租方式：□固定，合同期内年租金不作调整；□随基准地价调整。", []),
    ("固定 / 随基准地价调整；计租方式为随基准地价调整的，新标准生效后调整。", []),
    ("□固定；☑随基准地价调整；固定时不作调整。", ["随标准调整"]),
    ("☑固定，合同期内年租金不作调整；□随基准地价调整。", ["固定"]),
    ("☑固定；☑随基准地价调整。", ["固定", "随标准调整"]),
])
def test_ocr_mode_requires_actual_selection(text, expected):
    result = prose_candidates([{"text": text}], "synthetic-page", "test-model", "test-provider")
    assert [r["value"] for r in result["mode"]] == expected
    for row in result["mode"]:
        assert row["raw_text"] in text  # Cite the visible checkbox, not invented explanatory text.


def test_contract_draft_rejects_missing_effective_rate(client):
    doc = upload(client, "no-rate", "white")
    pub = confirm(client, doc, fields(doc_type="新签"))
    response = client.post("/api/contracts/draft", json={"parcel_id": pub["parcel_id"], "kind": "新签",
        "start": "1900-01-01", "end": "1909-12-31"}, headers=HEAD)
    assert response.status_code == 400 and "无适用计租标准" in response.text


def test_cloud_protocol_cleanup_keeps_prose_without_fabricating_mode():
    text = "第一条甲方将位于测试街1号的国有土地面积10.50平方米。<|LOC_123|>\n□固定，合同期内不作调整；□随基准地价调整"
    candidates, diagnostics = governance.parse_ocr_text(text, {"id": "synthetic"},
                                                      {"model_id": "test-model", "provider": "test"})
    assert [c["value"] for c in candidates["area"]] == ["10.50"]
    assert not candidates["mode"]
    assert diagnostics["location_markers_removed_from_extraction"] == 1
    assert candidates["area"][0]["bbox"] is None


def test_legacy_cloud_contract_type_alias():
    from app.providers import parse_reply

    candidates = parse_reply({"fields": [{"field": "contract_type", "value": "变更", "raw_text": "变更协议",
                                            "bbox": [0.1, 0.1, 0.2, 0.2]}]},
                             {"id": "synthetic"}, [0, 0, 100, 100], {"model_id": "test", "provider": "test"})
    assert candidates["doc_type"][0]["value"] == "变更"
    assert "contract_type" not in candidates


def test_private_multi_page_primary_does_not_authorize_public_secondary(tmp_path, monkeypatch):
    import json

    import httpx
    from PIL import Image

    from app.cloud_cascade import run_cloud_cascade

    monkeypatch.setenv("FDE_TEST_KEY", "synthetic-test-only")
    path = tmp_path / "private.png"
    Image.new("RGB", (100, 100), "white").save(path)
    pages = [{"id": "private-page", "path": str(path), "source": str(path)}]
    config = {"enabled": True, "document_context": True, "authorized_scope": "explicit_sample_hash",
              "models": {
                  "primary": {"authorized": True, "provider": "local", "model_id": "primary",
                              "base_url": "http://127.0.0.1:9999/v1", "api_key_env": ""},
                  "secondary": {"authorized": True, "provider": "public", "model_id": "secondary",
                                "base_url": "https://mock.invalid/v1", "api_key_env": "FDE_TEST_KEY"}}}
    calls = []

    def handler(request):
        calls.append(str(request.url))
        assert request.url.host == "127.0.0.1"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"model": "primary", "choices": [{"message": {"content": json.dumps({
            "fields": [{"field": "area", "value": "10", "raw_text": "面积10", "page_id": "private-page",
                        "bbox": [0.1, 0.1, 0.2, 0.2]}]})}}]})

    result = run_cloud_cascade(pages, tmp_path / "run", config=config, transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert result["pages"][0]["stages"][0]["status"] == "failed"
    assert "授权外发范围" in result["pages"][0]["stages"][0]["reason"]


def test_registered_contract_draft_uses_displayed_area(client, monkeypatch):
    registry = {"不动产单元号": "9-9-999", "权利性质": "租赁", "坐落": "测试街1号", "权利人": "模拟测试人",
                "宗地面积（㎡）": "30", "证件号码": "synthetic-company", "联系电话": "10000000000"}
    monkeypatch.setattr(main, "SOURCE", {**main.SOURCE, "registry": [registry]})
    doc = upload(client, "registered-draft", "white")
    pub = confirm(client, doc, fields(doc_type="新签", area="10", mode="随标准调整"))
    response = client.post("/api/contracts/draft", json={"parcel_id": pub["parcel_id"], "kind": "续期",
        "start": "2027-01-01", "end": "2036-12-31"}, headers=HEAD)
    assert response.status_code == 200, response.text
    values = response.json()["values"]
    assert Decimal(values["土地面积"]) == 30
    assert Decimal(values["年租金小写"]) == Decimal(values["土地面积"]) * Decimal(values["年租金标准"]) == 72
