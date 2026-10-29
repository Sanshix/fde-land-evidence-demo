"""Human decisions survive hold/resume and remain bound to the publication preview."""

import json

import pytest
from test_api import HEAD, new_doc, preview, reviewed
from test_api import client as client_fixture

from app import store

client = client_fixture


def test_hold_resume_retains_draft_without_cloud_or_publication(client):
    doc, _ = new_doc(client)
    body = reviewed(doc) | {"alteration_review": {"decision": "unknown", "unit_id": doc["pages"][0]["id"]}}
    body["fields"].update(altered_field="土地面积", altered_before="8", altered_sealed="无法判断")
    assert client.post(f"/api/documents/{doc['id']}/draft", json=body, headers=HEAD).status_code == 200
    assert client.post(f"/api/documents/{doc['id']}/hold", json={"version": doc["version"], "reason": "签章无法判断"}, headers=HEAD).status_code == 200
    held = client.get(f"/api/documents/{doc['id']}").json()
    assert held["draft"]["fields"]["altered_before"] == "8"
    resumed = client.post(f"/api/documents/{doc['id']}/resume-review", json={"version": held["version"], "reason": "对照原件继续核验"}, headers=HEAD)
    assert resumed.status_code == 200 and resumed.json()["new_cloud_calls"] == 0
    current = client.get(f"/api/documents/{doc['id']}").json()
    assert current["state"] == "needs_human" and current["draft"]["alteration_review"]["decision"] == "unknown"
    assert current["result"] == doc["result"] and client.get('/api/ledger').json() == []
    assert client.post(f"/api/documents/{doc['id']}/resume-review", json={"version": current["version"], "reason": "重复"}, headers=HEAD).status_code == 409


@pytest.mark.parametrize('review,seal', [({"decision": "unknown", "unit_id": "missing"}, "无法判断"),
                                       ({"decision": "present", "unit_id": "another-document"}, "是"),
                                       ({"decision": "absent", "unit_id": "CURRENT"}, "是")])
def test_seal_undecided_foreign_source_or_conflict_cannot_preview(client, review, seal):
    doc, _ = new_doc(client)
    if review['unit_id'] == 'CURRENT':
        review = {**review, 'unit_id': doc['pages'][0]['id']}
    body = reviewed(doc) | {"alteration_review": review}
    body['fields'].update(altered_field='土地面积', altered_before='8', altered_sealed=seal)
    result = client.post(f"/api/documents/{doc['id']}/preview", json=body, headers=HEAD)
    assert result.status_code == 200 and result.json()['errors']


def test_explicit_seal_decision_is_saved_and_preview_bound(client):
    doc, _ = new_doc(client)
    body = reviewed(doc) | {"alteration_review": {"decision": "absent", "unit_id": doc['pages'][0]['id']}}
    body['fields'].update(altered_field='土地面积', altered_before='8', altered_sealed='否')
    body['citations'].update({key: doc['pages'][0]['id'] for key in ['altered_field', 'altered_before']})
    body = preview(client, doc, body)
    changed = {**body, 'alteration_review': {**body['alteration_review'], 'reason': 'changed after preview'}}
    assert client.post(f"/api/documents/{doc['id']}/confirm", json=changed, headers=HEAD).status_code == 409
    response = client.post(f"/api/documents/{doc['id']}/confirm", json=body, headers=HEAD)
    assert response.status_code == 200, response.text
    saved = client.get(f"/api/documents/{doc['id']}").json()
    assert saved['review']['alteration_review'] == body['alteration_review']


def test_recovery_options_are_explicit_and_persisted_without_changing_global_config(client):
    doc, _ = new_doc(client)
    with store.connection() as db:
        db.execute("UPDATE jobs SET state='unknown' WHERE document_id=?", (doc['id'],))
    url = f"/api/documents/{doc['id']}/retry"
    assert client.post(url, json={'version': doc['version'], 'retry_unknown': True}, headers=HEAD).status_code == 400
    assert client.post(url, json={'version': doc['version'], 'retry_unknown': []}, headers=HEAD).status_code == 400
    response = client.post(url, json={'version': doc['version']}, headers=HEAD)
    assert response.status_code == 200
    options = json.loads((store.DATA / 'runs' / doc['id'] / (response.json()['job'] + '-options.json')).read_text())
    assert options['retry_failed'] and not options['retry_unknown']


def test_saved_human_draft_cancels_pending_background_without_new_read(client, monkeypatch):
    from app import main

    doc, _ = new_doc(client)
    calls = []

    def fake(*args, **kwargs):
        calls.append('primary')
        return {'mode': 'test_stub', 'elapsed_seconds': 0, 'risks': [], 'fallback_pending': True}

    monkeypatch.setattr(main, 'run_cloud_cascade', fake)
    main.worker_once()
    current = client.get(f"/api/documents/{doc['id']}").json()
    assert client.post(f"/api/documents/{doc['id']}/draft", json=reviewed(current), headers=HEAD).status_code == 200
    main.worker_once(fallback=True)
    after = client.get(f"/api/documents/{doc['id']}").json()
    assert calls == ['primary'] and after['draft']['fields']['tenant'] == '模拟测试人'
    with store.connection() as db:
        assert db.execute('SELECT state FROM jobs WHERE document_id=?', (doc['id'],)).fetchone()[0] == 'cancelled'


def test_human_draft_saved_during_read_is_not_invalidated(client, monkeypatch):
    import threading

    from app import main

    doc, _ = new_doc(client)
    entered, release = threading.Event(), threading.Event()

    def fake(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return {'mode': 'test_stub', 'elapsed_seconds': 0, 'risks': [], 'fallback_pending': False}

    monkeypatch.setattr(main, 'run_cloud_cascade', fake)
    thread = threading.Thread(target=main.worker_once)
    thread.start()
    try:
        assert entered.wait(2)
        current = client.get(f"/api/documents/{doc['id']}").json()
        assert client.post(f"/api/documents/{doc['id']}/draft", json=reviewed(current), headers=HEAD).status_code == 200
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    after = client.get(f"/api/documents/{doc['id']}").json()
    assert after['state'] == 'needs_human' and after['draft']['fields']['tenant'] == '模拟测试人'
    assert after['result'] is None
