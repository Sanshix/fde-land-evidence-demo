from test_api import HEAD, client, new_doc, reviewed, preview
from app import store

ADMIN = {**HEAD, 'X-FDE-Role': 'admin', 'X-FDE-Actor': 'administrator'}


def publish(c):
    d, _ = new_doc(c)
    b = preview(c, d, reviewed(d))
    r = c.post(f"/api/documents/{d['id']}/confirm", json=b, headers=HEAD)
    assert r.status_code == 200, r.text
    return c.get('/api/documents/' + d['id']).json(), r.json()


def test_escalation_gate_return_resubmit_and_archive(client):
    d, _ = new_doc(client)
    with store.connection() as db:
        db.execute("UPDATE documents SET state='needs_human' WHERE id=?", (d['id'],))
    path = f"/api/documents/{d['id']}"
    b = preview(client, d, reviewed(d))
    assert client.post(path+'/approval', json={'version':d['version'],'decision':'submit','reason':'需审批'},headers=HEAD).status_code == 200
    assert client.post(path+'/confirm',json=b,headers=HEAD).status_code == 403
    assert client.post(path+'/approval',json={'version':d['version'],'decision':'return','reason':'补依据'},headers=HEAD).status_code == 403
    assert client.post(path+'/approval',json={'version':d['version'],'decision':'return','reason':'补依据'},headers=ADMIN).status_code == 200
    assert client.post(path+'/confirm',json=b,headers=ADMIN).status_code == 409
    assert client.post(path+'/approval',json={'version':d['version'],'decision':'submit','reason':'已补正'},headers=HEAD).status_code == 200
    assert client.post(path+'/confirm',json=b,headers=ADMIN).status_code == 200
    assert client.get('/api/approvals').json()[0]['status'] == 'archived'


def test_revision_prefill_atomic_replace_and_stale_checks(client):
    d,p = publish(client)
    parcel=p['parcel_id']; path=f"/api/documents/{d['id']}/revision"
    assert client.post(path,json={'version':d['version']},headers=HEAD).status_code == 403
    assert client.post(f'/api/parcels/{parcel}/check',json={},headers=HEAD).status_code == 403
    result=client.post(f'/api/parcels/{parcel}/check',json={},headers=ADMIN)
    assert result.status_code == 200,result.text
    assert client.get(f'/api/parcels/{parcel}/check').json()['stale'] is False
    revision=client.post(path,json={'version':d['version']},headers=ADMIN).json()
    r=client.get('/api/documents/'+revision['id']).json()
    assert r['draft']['fields'] == d['review']['fields']
    assert client.get('/api/ledger').json()[0]['id'] == p['id']
    body=reviewed(r);body['fields']['phone']='13800138000'
    body=preview(client,r,body)
    response=client.post(f"/api/documents/{r['id']}/confirm",json=body,headers=ADMIN)
    assert response.status_code == 200,response.text
    assert response.json()['payload']['due'] == p['payload']['due']
    assert client.get('/api/documents/'+d['id']).json()['state']=='withdrawn'
    contracts=client.get(f'/api/parcels/{parcel}/confirmed-contracts').json()
    assert [c['id'] for c in contracts] == [r['id']]
    assert contracts[0]['confirmed']=='已确认'
    assert contracts[0]['fields']['phone']=='13800138000'
    assert client.get(f'/api/parcels/{parcel}/check').json()['stale'] is True


def test_pending_revision_blank_confirmation_and_document_check(client, monkeypatch):
    from app import approval
    monkeypatch.setattr(approval, 'model_review', lambda docs, result: {'status':'completed','message':'测试模型建议，须人工核对'})
    d,p=publish(client)
    rid=client.post(f"/api/documents/{d['id']}/revision",json={'version':d['version']},headers=ADMIN).json()['id']
    rows=client.get(f"/api/parcels/{p['parcel_id']}/confirmed-contracts").json()
    assert {r['id']:r['confirmed'] for r in rows} == {d['id']:'已确认',rid:''}
    path=f'/api/documents/{rid}/check'
    assert client.post(path,json={},headers=HEAD).status_code==403
    response=client.post(path,json={},headers=ADMIN)
    assert response.status_code==200,response.text
    assert response.json()['model_review']['status']=='completed'
    assert client.get('/api/ledger').json()[0]['id']==p['id']


def test_model_unavailable_explicitly_reported(monkeypatch):
    from app import approval, providers
    monkeypatch.setattr(providers, 'configuration', lambda: {'enabled':False})
    assert approval.model_review([], {})['status']=='unavailable'


def test_revision_preserves_supplemental_payment_events(client):
    d,p=publish(client)
    parcel=p['parcel_id']
    event={'publication':p['id'],'kind':'payment','reason':'Synthetic evidence','evidence_confirmed':True,
           'evidence_document':d['id'],'date':'2026-06-01','year':2026,'amount':'30','receipt':'REVISION-PAYMENT'}
    ep=client.post(f'/api/parcels/{parcel}/events/preview',json=event,headers=HEAD)
    assert ep.status_code==200,ep.text
    confirmed=client.post(f'/api/parcels/{parcel}/events/confirm',json={**event,'confirmed':True,'preview_hash':ep.json()['preview_hash']},headers=HEAD)
    assert confirmed.status_code==200,confirmed.text
    for _ in range(2):
        rid=client.post(f"/api/documents/{d['id']}/revision",json={'version':d['version']},headers=ADMIN).json()['id']
        r=client.get('/api/documents/'+rid).json()
        body=preview(client,r,reviewed(r))
        response=client.post(f'/api/documents/{rid}/confirm',json=body,headers=ADMIN)
        assert response.status_code==200,response.text
        assert response.json()['payload']['paid']=='30.00'
        d=client.get('/api/documents/'+rid).json()
