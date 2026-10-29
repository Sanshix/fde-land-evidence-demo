"""Workflow tests use synthetic documents; do not measure OCR accuracy."""
import json

from test_api import HEAD, new_doc, preview, reviewed
from test_api import client as client_fixture

from app import store

client = client_fixture


def published(client):
    doc, _ = new_doc(client)
    b = preview(client, doc, reviewed(doc))
    pub = client.post(f"/api/documents/{doc['id']}/confirm", json=b, headers=HEAD)
    assert pub.status_code == 200, pub.text
    return doc, pub.json()


def test_new_parcel_draft_does_not_create_ledger(client):
    body = {'kind': '新签', 'start': '2026-01-01', 'end': '2027-12-31', 'fields': {
        'tenant': '合成测试人', 'identity': 'test', 'phone': '13800000000', 'parcel_no': 'test-01',
        'address': '测试路1号', 'area': '10', 'use': '住宅', 'grade': '一级', 'mode': '固定'}}
    r = client.post('/api/contracts/draft', json=body, headers=HEAD)
    assert r.status_code == 200, r.text
    assert r.json()['status'] == 'pending_confirmation'
    detail = client.get(f"/api/contracts/{r.json()['id']}").json()
    assert detail['status'] == 'pending_confirmation'
    assert detail['values'] == r.json()['values']
    assert client.get('/api/ledger').json() == []
    assert len(client.get('/api/contracts').json()) == 1
    assert client.get(f"/api/contracts/{r.json()['id']}/document").status_code == 200
    body['kind'] = '续期'
    assert client.post('/api/contracts/draft', json=body, headers=HEAD).status_code == 404


def test_payment_preview_confirmation_and_duplicate_receipt(client):
    doc, pub = published(client)
    data = {'kind': 'payment', 'publication': pub['id'], 'amount': '10', 'year': 2026, 'date': '2026-09-01',
            'receipt': 'SYNTHETIC-001', 'evidence_confirmed': True, 'evidence_document': doc['id'], 'reason': 'synthetic payment evidence'}
    route = f"/api/parcels/{pub['parcel_id']}/events"
    p = client.post(route + '/preview', json=data, headers=HEAD)
    assert p.status_code == 200, p.text
    before = p.json()['before']['payload']
    assert float(p.json()['after']['paid']) == float(before['paid']) + 10
    assert client.post(route + '/confirm', json=data, headers=HEAD).status_code == 400
    r = client.post(route + '/confirm', json=data | {'confirmed': True, 'preview_hash': p.json()['preview_hash']}, headers=HEAD)
    assert r.status_code == 200, r.text
    data['publication'] = r.json()['id']
    assert client.post(route + '/preview', json=data, headers=HEAD).status_code == 400
    assert client.get('/api/stats').json()['累计实缴'] == r.json()['payload']['paid']


def test_termination_and_stale_preview(client):
    doc, pub = published(client)
    body = {'kind': 'termination', 'publication': pub['id'], 'date': '2026-06-30',
            'evidence_confirmed': True, 'evidence_document': doc['id'], 'reason': 'synthetic termination evidence'}
    route = f"/api/parcels/{pub['parcel_id']}/events"
    p = client.post(route + '/preview', json=body, headers=HEAD)
    assert p.status_code == 200, p.text
    assert float(p.json()['after']['due']) < float(p.json()['before']['payload']['due'])
    result = client.post(route + '/confirm', json=body | {'confirmed': True, 'preview_hash': p.json()['preview_hash']}, headers=HEAD)
    assert result.status_code == 200, result.text
    assert result.json()['payload']['status'] == '已解除/收回'
    assert client.post(route + '/confirm', json=body | {'confirmed': True, 'preview_hash': p.json()['preview_hash']}, headers=HEAD).status_code == 409


def test_sms_gates_and_idempotent_simulation(client, tmp_path, monkeypatch):
    config = tmp_path / 'notice.json'
    config.write_text(json.dumps({'缴费地点': '测试大厅', '经办电话': '12345'}))
    monkeypatch.setenv('FDE_NOTICE_CONFIG', str(config))
    _, pub = published(client)
    n = client.post(f"/api/notices/{pub['id']}", json={}, headers=HEAD).json()
    route = f"/api/notices/{n['id']}"
    assert client.post(route + '/send', json={}, headers=HEAD).status_code == 400
    client.post(route + '/events', json={'status': '已审核', 'note': 'checked'}, headers=HEAD)
    # The synthetic contract fixture has no valid phone; patch only the fixture before notice creation.
    with store.connection() as db:
        p = dict(pub['payload'], phone='13800000000')
        new = store.publish(db, pub['parcel_id'], p, 'synthetic')
    n = client.post(f"/api/notices/{new['id']}", json={}, headers=HEAD).json()
    route = f"/api/notices/{n['id']}"
    client.post(route + '/events', json={'status': '已审核', 'note': 'checked'}, headers=HEAD)
    first = client.post(route + '/send', json={'outcome': 'timeout'}, headers=HEAD)
    assert first.status_code == 200, first.text
    assert first.json()['status'] == '结果未知'
    again = client.post(route + '/send', json={'outcome': 'success'}, headers=HEAD)
    assert again.json()['id'] == first.json()['id']
    assert len(client.get(route + '/tasks').json()) == 1
    receipt = {'receipt_id': 'synthetic-receipt', 'status': 'success'}
    assert client.post(route + '/simulation-receipt', json=receipt, headers=HEAD).status_code == 200
    assert client.post(route + '/simulation-receipt', json=receipt, headers=HEAD).status_code == 200
    assert len(client.get(route + '/tasks').json()[0]['receipts']) == 1
    assert client.post(route + '/simulation-receipt', json=receipt | {'status': 'failure'}, headers=HEAD).status_code == 409
    assert client.post(route + '/send', json={'channel': 'real'}, headers=HEAD).status_code == 400


def test_reminder_view_date_and_tax_preview_conflict(client):
    published(client)
    assert client.get('/api/reminders?today=invalid').status_code == 400
    early = client.get('/api/reminders?today=2026-01-01').json()
    assert all(r['kind'] != '合同到期' for r in early)
    p = client.post('/api/tax-preview', json={'year': 2026, 'quarter': 3}, headers=HEAD)
    assert p.status_code == 200, p.text
    r = client.post('/api/tax-push', json={'year': 2026, 'quarter': 3, 'preview_hash': 'stale'}, headers=HEAD)
    assert r.status_code == 409
    assert client.post('/api/tax-push', json={'year': 2026, 'quarter': 3, 'preview_hash': p.json()['preview_hash']}, headers=HEAD).status_code == 200


def test_rule_policy_and_all_new_writes_enforce_role(client):
    doc, pub = published(client)
    admin = HEAD | {'X-FDE-Role': 'admin'}
    rates = client.get('/api/rules').json()['rates']
    invalid = client.post('/api/rules/preview', json={'rates': rates, 'policy': {'segmentation': 'daily'}}, headers=admin)
    assert invalid.status_code == 400
    policy = {'rounding': 'answer_key'}
    p = client.post('/api/rules/preview', json={'rates': rates, 'policy': policy}, headers=admin)
    assert p.status_code == 200, p.text
    r = client.post('/api/rules/confirm', json={'rates': rates, 'policy': policy, 'reason': 'synthetic policy regression',
        'draft_hash': p.json()['draft_hash'], 'confirm_calculation': True}, headers=admin)
    assert r.status_code == 200, r.text
    assert client.get('/api/rules').json()['policy']['rounding'] == 'answer_key'
    viewer = HEAD | {'X-FDE-Role': 'viewer'}
    for route in ['/api/contracts/draft', '/api/tax-preview', '/api/notices/unknown/send',
                  f"/api/parcels/{pub['parcel_id']}/events/preview"]:
        assert client.post(route, json={}, headers=viewer).status_code == 403


def test_task_and_operation_log_preserve_initiator_role(client):
    doc, _ = new_doc(client)
    status = client.get('/api/status').json()
    job = next(j for j in status['jobs'] if j['document_id'] == doc['id'])
    assert job['actor'] == HEAD['X-FDE-Actor']
    assert job['role'] == 'reviewer'
    assert job['requested_at'] > 0
    entry = next(r for r in status['operations_log'] if r['target'] == doc['id'] and r['action'] == 'import')
    assert json.loads(entry['payload'])['operation_role'] == 'reviewer'
    # Viewing as a different role must not relabel past operations.
    status = client.get('/api/status', headers={'X-FDE-Role': 'admin'}).json()
    assert status['jobs'][0]['role'] == 'reviewer'
