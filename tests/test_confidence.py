"""Synthetic evidence checks; these tests do not measure OCR accuracy."""
import copy
import json

import pytest
from test_api import client, HEAD, reviewed, preview
from test_pr_acceptance import candidate, batch_rows, payments
from test_domain import fields
from app import confidence, store
from app.triage import triage


def document(score=.99):
    values = fields(doc_type='新签')
    return {'id': 'synthetic', 'state': 'needs_human', 'pages': [{'id': 'p', 'width': 200, 'height': 200}],
            'result': {'completion_status': 'completed', 'candidates': {
                key: [dict(value=value, raw_text=value, model=model, kind='ocr',
                           source_page_id='p', bbox=[1, 1, 100, 100], score=score)
                      for model in ['a', 'b']] for key, value in values.items()}}, 'intake': {}}


def test_high_requires_key_fields_not_just_average():
    doc = document()
    assert triage(doc)['eligible']
    high = confidence.machine(doc)
    assert high['score'] > 95 and high['level'] == 'high'
    for c in doc['result']['candidates']['annual']:
        c['score'] = .45
    profile = confidence.machine(doc)
    assert profile['minimum_critical']['key'] == 'annual'
    assert profile['level'] == 'low'
    assert not triage(doc)['eligible']


def test_unknown_missing_and_context_only_are_never_high():
    for mode in ['unknown', 'missing', 'context']:
        doc = document()
        rows = doc['result']['candidates']['annual']
        if mode == 'unknown':
            for c in rows:
                c['score'] = .99; c['score_meaning'] = 'not_provided'
        elif mode == 'context':
            for c in rows:
                c['kind'] = 'context_suggestion'
        else:
            doc['result']['candidates'].pop('annual')
        p = confidence.machine(doc)
        assert 'annual' in p['fields']
        assert p['fields']['annual']['score'] < 90
        assert not triage(doc)['eligible']
        assert p['fields']['annual']['weight'] > 0


def test_conflict_missing_anchor_and_alteration_caps():
    doc = document()
    doc['result']['candidates']['annual'][1]['value'] = '999'
    assert confidence.machine(doc)['fields']['annual']['score'] <= 49
    assert not triage(doc)['eligible']
    doc = document()
    for c in doc['result']['candidates']['annual']:
        c['bbox'] = [0, 0, 1000, 1000]
    assert confidence.machine(doc)['fields']['annual']['score'] < 90
    doc = document()
    doc['result']['candidates']['annual'][0]['text_role'] = 'crossed_out'
    assert confidence.machine(doc)['fields']['annual']['score'] <= 49


def test_applicability_and_optional_contract_number():
    doc = document()
    doc['result']['candidates'].pop('contract_no', None)
    assert 'contract_no' not in confidence.machine(doc)['fields']
    assert triage(doc)['eligible']
    for c in doc['result']['candidates']['mode']:
        c['value'] = '随标准调整'; c['raw_text'] = '随标准调整'
    doc['result']['candidates'].pop('annual')
    p = confidence.machine(doc)
    assert 'annual' not in p['fields']
    assert sum(f['weight'] for f in p['fields'].values()) == pytest.approx(1)
    doc['result']['candidates'] = {k: v for k,v in doc['result']['candidates'].items() if k in {'tenant','parcel_no'}}
    doc['result']['candidates']['doc_type'] = [dict(value='变更',raw_text='变更',model=m,kind='ocr',source_page_id='p',bbox=[1,1,20,20],score=.99) for m in ['a','b']]
    assert 'start' not in confidence.machine(doc)['fields']


def test_business_blocker_lowers_high_machine_profile_without_mutating_it():
    p = confidence.machine(document())
    assert p['level'] == 'high'
    lowered = confidence.restrict(p, [{'field': None, 'code': 'business_conflict', 'message': '宗地归属未定'}])
    assert lowered['score'] == p['score']
    assert lowered['level'] == 'low' and not lowered['batch_eligible']
    assert p['level'] == 'high'


def test_route_snapshot_and_verified_fields_survive_human_correction(client, monkeypatch):
    payments(monkeypatch, [])
    doc = candidate(client, 'score-snapshot', 'pink')
    assert doc['processing_state'] == 'pending_confirmation'
    assert doc['confidence']['level'] == 'high'
    assert any(d['processing_state'] == 'pending_confirmation' for d in client.get('/api/status').json()['documents'])
    profile = copy.deepcopy(doc['confidence'])
    b = reviewed(doc)
    # Use the source candidates' fields; human changes rent but must keep original machine score.
    b['fields'] = {k: rows[0]['value'] for k,rows in doc['result']['candidates'].items()}
    b['citations'] = doc['prefill']['citations']
    b['citation_details'] = doc['prefill']['citation_details']
    b['fields']['annual'] = '150'
    b = preview(client, doc, b)
    response = client.post(f"/api/documents/{doc['id']}/confirm", json=b, headers=HEAD)
    assert response.status_code == 200, response.text
    saved = client.get(f"/api/documents/{doc['id']}").json()
    assert saved['confidence']['score'] == profile['score']
    assert saved['field_verification']['annual']['status'] == 'corrected'
    assert saved['field_verification']['annual']['original_value'] == '120'
    assert saved['field_verification']['annual']['confirmed_value'] == '150'
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM machine_assessments WHERE document_id=? AND digest=?', (doc['id'], profile['source_digest'])).fetchone()[0] == 1
        assert db.execute('SELECT processing_state FROM routing_assessments WHERE document_id=?', (doc['id'],)).fetchone()[0] == 'published'
        with pytest.raises(Exception, match='immutable history'):
            db.execute('DELETE FROM machine_assessments WHERE document_id=?', (doc['id'],))


def test_weak_payment_consistent_between_list_and_batch(client, monkeypatch):
    from test_pr_acceptance import payment
    payments(monkeypatch, [payment(parcel=None)])
    doc = candidate(client, 'weak-source', 'orange', values=fields(doc_type='新签',tenant='同一承租人',contract_no='weak-source'))
    assert doc['processing_state'] == 'needs_human'
    assert doc['confidence']['level'] == 'low'
    assert not batch_rows(client)[0]['eligible']


def test_machine_snapshot_invalidates_on_new_evidence(client):
    doc = candidate(client, 'new-evidence', 'yellow')
    old = doc['confidence']['source_digest']
    with store.connection() as db:
        count = db.execute('SELECT COUNT(*) FROM machine_assessments WHERE document_id=?',(doc['id'],)).fetchone()[0]
    result = doc['result']
    for c in result['candidates']['annual']:
        c['score'] = .4
    with store.connection() as db:
        db.execute('UPDATE documents SET result=?,version=version+1 WHERE id=?', (json.dumps(result), doc['id']))
    new = client.get('/api/documents/'+doc['id']).json()
    assert new['confidence']['source_digest'] != old
    assert new['processing_state'] == 'needs_human'
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM machine_assessments WHERE document_id=?',(doc['id'],)).fetchone()[0] == count + 1


def test_medium_routes_to_individual_review():
    p = triage(document(.8))
    assert p['confidence']['level'] == 'medium'
    assert p['confidence']['score'] == pytest.approx(87)
    assert not p['eligible']


def test_manual_added_facts_have_separate_human_status():
    p = confidence.machine(document())
    v = confidence.verification(p, {'lessor':'人工核实的甲方'})
    assert v['lessor']['status'] == 'added'
    assert v['lessor']['original_score'] is None


def test_batch_endpoint_rechecks_scores_instead_of_trusting_previous_high_route(client, monkeypatch):
    from test_pr_acceptance import batch_publish
    payments(monkeypatch, [])
    doc = candidate(client, 'no-confidence-bypass', 'cyan')
    rows = batch_rows(client)
    assert rows[0]['eligible']
    result = doc['result']
    for c in result['candidates']['annual']:
        c['score'] = .3
    with store.connection() as db:
        db.execute('UPDATE documents SET result=? WHERE id=?',(json.dumps(result), doc['id']))
    reply = batch_publish(client, rows)
    assert reply.status_code == 200
    assert reply.json()['published'] == 0
    assert client.get('/api/ledger').json() == []
