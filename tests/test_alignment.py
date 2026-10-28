"""Requirement-alignment tests: 赛题表2 业务闭环 + 口径第十一/十二条 + 练习集评分回归。"""

import csv
import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import exercise_pipeline
import pytest
from PIL import Image
from test_api import HEAD, client, preview  # noqa: F401  (fixture re-export)
from test_domain import fields

from app import business, domain, providers

EXERCISE = business.SAMPLES
ANSWERS = EXERCISE / "08_标准答案"


def upload(client, name, color):  # noqa: F811
    buffer = io.BytesIO()
    Image.new("RGB", (60, 60), color).save(buffer, format="PNG")
    r = client.post(
        "/api/import", files={"files": (name + ".png", buffer.getvalue(), "image/png")}, data={"name": name}, headers=HEAD
    )
    assert r.status_code == 200, r.text
    return client.get("/api/documents/" + r.json()["id"]).json()


def confirm(client, doc, f, **extra):  # noqa: F811
    body = {
        "version": doc["version"],
        "fields": f,
        "citations": {k: doc["pages"][0]["id"] for k in f},
        "payment_ids": [],
        "relief_ids": [],
        "reason": "synthetic workflow fixture",
        "evidence_clear": True,
        "material_complete": True,
        "confirm_calculation": True,
        "confirm_payments": True,
        **extra,
    }
    body = preview(client, doc, body)
    r = client.post(f"/api/documents/{doc['id']}/confirm", json=body, headers=HEAD)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.skipif(not (ANSWERS / "合同要素.csv").is_file(), reason="练习集标准答案不在本机")
@pytest.mark.parametrize("rounding,minimum", [("half_up", 96.0), ("answer_key", 100.0)])
def test_exercise_rule_engine_against_answer_key(tmp_path, monkeypatch, rounding, minimum):
    """已确认要素 → 归宗 → 缴费候选 → 核算 → 问题清单，用出题方 score.py 打分（识别准确率另测）。"""
    monkeypatch.setenv("FDE_ROUNDING", rounding)
    result = exercise_pipeline.run(ANSWERS, tmp_path)
    assert not result["errors"]
    output = subprocess.run(
        [sys.executable, str(EXERCISE / "score.py"), str(ANSWERS), str(tmp_path)],
        capture_output=True, text=True, check=True,
    ).stdout
    total = float(re.search(r"总分 ([\d.]+)", output).group(1))
    assert total >= minimum, output
    assert "问题清单  F1 100.0%" in output and "存疑待确认单算 F1 100.0%" in output
    expected = {r["说明"] for r in csv.DictReader(open(ANSWERS / "问题清单.csv", encoding="utf-8-sig"))
                if r["问题类型"] == "存疑待确认"}
    assert expected == {i["说明"] for i in result["issues"] if i["问题类型"] == "存疑待确认"}


def test_change_agreement_and_original_contract_link_merge_parcels(client):  # noqa: F811
    first = upload(client, "A-new", "white")
    a = fields(contract_no="甲字001", doc_type="新签", start="2020-01-01", end="2029-12-31", signed="2019-12-20")
    confirm(client, first, a)
    second = upload(client, "B-change", "black")
    # 变更协议宗地号写错一位，靠“原合同编号”归到同一宗地
    b = {"doc_type": "变更", "contract_no": "甲变001", "original_contract_no": "甲字001", "tenant": "受让人乙",
         "parcel_no": "9-9-998", "address": "测试街1号", "signed": "2024-05-01", "phone": "10000000001"}
    confirm(client, second, b)
    ledger = client.get("/api/ledger").json()
    assert len(ledger) == 1
    payload = ledger[0]["payload"]
    assert sorted(payload["documents"]) == sorted([first["id"], second["id"]])
    assert payload["recipient"] == "受让人乙" and payload["phone"] == "10000000001"
    assert any(i["type"] == "登记数据中无对应宗地" for i in payload["issues"])


def test_reminders_stats_and_export_schema(client):  # noqa: F811
    doc = upload(client, "LX-TEST", "white")
    confirm(client, doc, fields(start="2016-01-01", end="2025-12-31", phone=""))
    items = client.get("/api/reminders").json()
    kinds = {(i["kind"], i["level"]) for i in items}
    assert ("合同到期", "红") in kinds and ("欠缴", "红") in kinds
    assert "BEGIN:VEVENT" in client.get("/api/reminders?format=ics").text
    stats = client.get("/api/stats").json()
    assert stats["宗地数"] == 1 and stats["已到期未续"] == 1 and stats["累计收缴率"] == "0.0%"
    assert client.get("/api/stats?format=csv").status_code == 200
    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/export").content))
    for name in ("合同要素.csv", "宗地台账.csv", "问题清单.csv"):
        header = archive.read(name).decode("utf-8-sig").splitlines()[0].split(",")
        if (ANSWERS / name).is_file():
            expected = open(ANSWERS / name, encoding="utf-8-sig").readline().strip().split(",")
            assert header[: len(expected)] == expected
    issues = archive.read("问题清单.csv").decode("utf-8-sig")
    assert "合同到期未续" in issues and "未填联系电话" in issues


def test_contract_draft_notice_and_tracking(client):  # noqa: F811
    doc = upload(client, "LX-TEST", "white")
    pub = confirm(client, doc, fields(start="2016-01-01", end="2025-12-31", contract_no="甲字009"))
    draft = client.post("/api/contracts/draft", json={"parcel_id": pub["parcel_id"], "kind": "续期",
                                                      "start": "2026-01-01", "end": "2035-12-31"}, headers=HEAD)
    assert draft.status_code == 200, draft.text
    text = zipfile.ZipFile(io.BytesIO(client.get(f"/api/contracts/{draft.json()['id']}/document").content)).read(
        "word/document.xml").decode()
    assert "【" not in text and "原合同（编号：甲字009）期满续签" in text and "2035-12-31" in text
    assert "贰拾肆元整" in text  # New lease uses 10 sqm × current 2.4, not the old fixed 120.
    notice = client.post(f"/api/notices/{pub['id']}", json={}, headers=HEAD).json()
    assert "2016至2026" in notice["sms_draft"] and notice["sms"]["segments"] == (2 if notice["sms"]["length"] > 70 else 1)
    assert client.post(f"/api/notices/{notice['id']}/events", json={"status": "已发送"}, headers=HEAD).status_code == 200
    assert client.get("/api/notices").json()[0]["status"] == "已发送"


def test_change_agreement_validation_and_document_attributes():
    assert domain.validate_fields({"doc_type": "变更", "tenant": "甲", "parcel_no": "1-2-3"}) == []
    assert domain.validate_fields(fields(mode="")) and not domain.validate_fields(fields(mode="未勾选"))
    assert domain.id_problem("990524194510197270") and domain.id_problem("990522520724245")
    assert domain.id_problem("99CH16HKMTK886Y9U7") is None
    assert domain.normalize_mode("☑随基准地价调整") == "随标准调整"


def test_origin_allowlist(client, monkeypatch):  # noqa: F811
    doc = upload(client, "X", "white")
    assert client.post(f"/api/documents/{doc['id']}/hold", json={"version": doc["version"], "reason": "x"},
                       headers=HEAD | {"Origin": "http://10.1.2.3:8765"}).status_code == 403
    monkeypatch.setenv("FDE_ALLOWED_ORIGINS", "http://10.1.2.3:8765")
    assert client.post(f"/api/documents/{doc['id']}/hold", json={"version": doc["version"], "reason": "x"},
                       headers=HEAD | {"Origin": "http://10.1.2.3:8765"}).status_code == 200


def test_private_model_endpoint_and_competition_scope(tmp_path):
    local = {"provider": "vllm", "model_id": "Qwen/Qwen2.5-VL-7B-Instruct", "authorized": True,
             "base_url": "http://10.0.0.5:8000/v1", "api_key_env": ""}
    assert providers.model_profile({"enabled": True, "models": {"primary": local}}, "primary")
    with pytest.raises(providers.ProviderUnavailable):
        providers.model_profile({"enabled": True, "models": {"primary": local | {"base_url": "http://example.com/v1"}}},
                                "primary")
    source = tmp_path / "x.png"
    source.write_bytes(b"not-a-real-contract")
    page = {"source": str(source), "path": str(source)}
    with pytest.raises(providers.ProviderUnavailable):
        providers.approved_input(page, {"authorized_scope": "explicit_sample_hash"})
    assert providers.approved_input(page, {"authorized_scope": "competition_simulated"})
    assert providers.approved_input(page, {"authorized_scope": "explicit_sample_hash"}, local)


def test_payment_candidates_cover_clerk_variants():
    source = business.sources()
    matches = business.suggest(
        {"parcel_no": "09-03-0116", "address": "××镇繁荣路二五八号", "tenant": "金秀华"}, source
    )
    places = {p["宗地（收费员录入）"] for p in matches["payments"]}
    assert {"繁荣路258", "繁荣路258号", "09-03-0116", None} <= places
    assert all(p["缴款人"] == "金秀华" for p in matches["payments"])
    assert business.address_key("××镇建设街199号", business.known_streets(source)) == "建设大街199"


def test_grade_from_street_and_unregistered_cadastre_key():
    source = business.sources()
    assert business.street_grade("××镇繁荣路82号", business.known_streets(source)) == "三级"
    assert business.parcel_key({"parcel_no": "7-3-100"}, source) == business.parcel_key(
        {"parcel_no": "000000007003GB00100F00071767"}, source
    )


def test_upper_money_roundtrip():
    for value in ["0.05", "10.00", "1005", "10010.01", "3998.25", "100000.1"]:
        assert domain.upper_money(domain.to_upper_money(value)) == domain.money(value)


def test_exercise_pipeline_module_is_importable():
    assert Path(exercise_pipeline.__file__).is_file()


def test_batch_confirm_only_takes_agreed_candidates(client):  # noqa: F811
    from app import store

    doc = upload(client, "LX-BATCH", "white")
    page = doc["pages"][0]["id"]
    good = fields(contract_no="甲字100", doc_type="新签", start="2020-01-01", end="2029-12-31")

    def cands(values, models=("model-a", "model-b")):
        return {k: [{"value": v, "model": m, "source_page_id": page, "raw_text": v, "bbox": [1, 1, 2, 2],
                     "score": 0.99}
                    for m in models] for k, v in values.items()}

    result = {"candidates": cands(good), "risks": [{"field": None, "code": "visual_review", "reason": "x"}]}
    with store.connection() as db:
        db.execute("UPDATE documents SET result=?,state='needs_human' WHERE id=?", (store.dumps(result), doc["id"]))
    single = upload(client, "LX-SINGLE", "black")
    with store.connection() as db:
        db.execute("UPDATE documents SET result=?,state='needs_human' WHERE id=?",
                   (store.dumps({"candidates": cands(good | {"contract_no": "甲字101", "parcel_no": "9-9-997"}, ("only",)),
                                 "risks": []}), single["id"]))
    rows = {r["name"]: r for r in client.get("/api/batch-candidates").json()}
    assert rows["LX-BATCH"]["eligible"] and not rows["LX-SINGLE"]["eligible"]
    docs = [{"id": r["id"], "version": r["version"], "draft_hash": r.get("draft_hash")} for r in rows.values()]
    out = client.post("/api/batch-confirm", json={"documents": docs, "reason": "批量确认", "reviewed_summary": True,
                      "evidence_clear": True, "material_complete": True, "confirm_calculation": True,
                      "confirm_payments": True},
                      headers=HEAD).json()
    assert out["published"] == 1
    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/export").content))
    assert "自动通过" in archive.read("宗地台账.csv").decode("utf-8-sig")


def test_retry_while_job_active_is_rejected(client):  # noqa: F811
    doc = upload(client, "RETRY", "white")
    r = client.post(f"/api/documents/{doc['id']}/retry", json={"version": doc["version"]}, headers=HEAD)
    assert r.status_code == 409  # 导入时已排队；再次重试会使运行中任务结果被丢弃


def test_review_values_are_normalized_before_validation():
    f = business.normalize_fields(fields(start="二〇二〇年一月一日", end="2029年12月31日", area="10.00平方米",
                                         annual="1,200元", mode="随基准地价调整"))
    assert (f["start"], f["end"], f["area"], f["annual"], f["mode"]) == (
        "2020-01-01", "2029-12-31", "10.00", "1200", "随标准调整")


def test_merge_misgrouped_pages_into_target_document(client):  # noqa: F811
    first = upload(client, "补档A", "white")
    second = upload(client, "补档A第2页", "black")
    from app import store

    with store.connection() as db:
        db.execute("UPDATE jobs SET state='done'")
        db.execute("UPDATE documents SET state='needs_human'")
    first = client.get("/api/documents/" + first["id"]).json()
    second = client.get("/api/documents/" + second["id"]).json()
    r = client.post(f"/api/documents/{second['id']}/merge-into",
                    json={"version": second["version"], "target": first["id"], "target_version": first["version"]},
                    headers=HEAD)
    assert r.status_code == 200, r.text
    merged = client.get("/api/documents/" + first["id"]).json()
    assert len(merged["pages"]) == 2 and client.get("/api/documents/" + second["id"]).json()["state"] == "withdrawn"
