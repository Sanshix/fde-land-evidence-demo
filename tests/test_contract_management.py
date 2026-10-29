from test_api import client, new_doc, reviewed, preview, HEAD

ADMIN = {"X-FDE-Role": "admin", "X-FDE-Actor": "admin"}


def test_withdrawal_requires_admin_review_and_retains_ledger(client):
    doc, _ = new_doc(client)
    b = preview(client, doc, reviewed(doc))
    assert client.post(f"/api/documents/{doc['id']}/confirm", json=b, headers=HEAD).status_code == 200
    current = client.get(f"/api/documents/{doc['id']}").json()
    url = f"/api/documents/{doc['id']}"
    body = {"version": current["version"], "reason": "原归属有误"}
    assert client.post(url + "/withdraw", json=body, headers=HEAD).status_code == 403
    assert client.post(url + "/withdraw", json=body, headers=ADMIN).status_code == 409
    assert client.post(url + "/withdraw-request", json=body, headers=HEAD).status_code == 200
    assert client.post(url + "/withdraw-request", json=body, headers=HEAD).status_code == 409
    assert len(client.get("/api/ledger").json()) == 1
    current = client.get(url).json()
    request_id = current["withdrawal_requests"][0]["id"]
    assert client.post(url + "/withdraw", json=body | {"request_id": request_id, "decision": "reject"}, headers=ADMIN).status_code == 200
    assert client.get(url).json()["state"] == "published"
    assert client.post(url + "/withdraw-request", json=body, headers=HEAD).status_code == 200
    request_id = client.get(url).json()["withdrawal_requests"][0]["id"]
    assert client.post(url + "/withdraw", json=body | {"request_id": request_id, "decision": "approve"}, headers=ADMIN).status_code == 200
    assert client.get(url).json()["state"] == "withdrawn"


def test_mode_needs_no_manual_citation_and_contract_number_optional(client):
    doc, _ = new_doc(client)
    b = reviewed(doc)
    b["fields"].pop("contract_no", None)
    b["citations"].pop("mode", None)
    b.get("citation_details", {}).pop("mode", None)
    b = preview(client, doc, b)
    response = client.post(f"/api/documents/{doc['id']}/confirm", json=b, headers=HEAD)
    assert response.status_code == 200, response.text


def test_prefill_uses_observed_candidates_and_preserves_draft(client):
    import json
    from app import store
    doc, _ = new_doc(client)
    unit = doc['pages'][0]['id']
    observed = {'value': '6800', 'raw_text': '6800', 'source_page_id': unit,
                'bbox': [0, 0, 50, 50], 'kind': 'ocr', 'model': 'synthetic', 'score': .95}
    guessed = observed | {'value': '猜测的出租人', 'kind': 'context_suggestion'}
    with store.connection() as db:
        db.execute('UPDATE documents SET result=? WHERE id=?',
                   (json.dumps({'candidates': {'annual': [observed], 'lessor': [guessed]}}), doc['id']))
    fetched = client.get(f"/api/documents/{doc['id']}").json()
    assert fetched['prefill']['fields']['annual'] == '6800'
    assert fetched['prefill']['citations']['annual'] == unit
    assert 'lessor' not in fetched['prefill']['fields']
