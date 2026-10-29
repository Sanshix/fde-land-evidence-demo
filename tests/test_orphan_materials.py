"""Synthetic material-chain acceptance, never an OCR accuracy fixture."""
import csv
import io
import zipfile

import pytest
from PIL import Image
from test_api import HEAD, new_doc, preview, reviewed
from test_api import client as client_fixture
from test_domain import fields

from app import main, store
from app.business import calculate_documents

client = client_fixture


def orphan_fields():
    return {'doc_type': '变更', 'original_contract_no': 'SYN-ORIGINAL',
            'tenant': '模拟受让方', 'parcel_no': '9-9-999', 'address': '测试街1号',
            'signed': '2026-06-01'}


def test_missing_original_is_not_zero_and_empty_withdrawal_is_zero(client):
    doc = {'id': 'synthetic-change', 'name': 'synthetic-change',
           'review': {'fields': orphan_fields(), 'evidence_clear': True}}
    with pytest.raises(ValueError, match='缺关联原合同，无法计租'):
        calculate_documents([doc], main.SOURCE)
    cleared = calculate_documents([], main.SOURCE)
    assert cleared['due'] == '0.00' and cleared['documents'] == []


def test_orphan_blocks_preview_publication_export_and_notice_then_links(client):
    doc, _ = new_doc(client)
    body = reviewed(doc)
    body.update(fields=orphan_fields(), citations={k: doc['pages'][0]['id'] for k in orphan_fields()})
    p = client.post(f"/api/documents/{doc['id']}/preview", json=body, headers=HEAD)
    assert p.status_code == 200
    assert any('缺关联原合同' in e for e in p.json()['errors'])
    assert p.json().get('calculation') is None and not p.json().get('draft_hash')
    # Stale/forged preview cannot bypass the calculation-layer guard.
    c = client.post(f"/api/documents/{doc['id']}/confirm", json=body | {'draft_hash': 'stale'}, headers=HEAD)
    assert c.status_code == 400 and '缺关联原合同' in c.text
    assert client.get('/api/ledger').json() == []
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM publications').fetchone()[0] == 0
    exported = zipfile.ZipFile(io.BytesIO(client.get('/api/export').content))
    assert list(csv.DictReader(io.StringIO(exported.read('宗地台账.csv').decode('utf-8-sig')))) == []
    assert client.post('/api/notices/1', json={}, headers=HEAD).status_code == 404
    assert client.get('/api/notices').json() == []
    # A different original fixture arrives later, with explicit contract number.
    image = Image.new('RGB', (101, 100), 'white')
    data = io.BytesIO()
    image.save(data, format='PNG')
    imported = client.post('/api/import', files={'files': ('original.png', data.getvalue(), 'image/png')},
                           data={'name': 'SYNTHETIC-ORIGINAL'}, headers=HEAD).json()
    original = client.get('/api/documents/' + imported['id']).json()
    original_body = reviewed(original)
    original_body['fields'] = fields(contract_no='SYN-ORIGINAL')
    original_body['citations'] = {k: original['pages'][0]['id'] for k in original_body['fields']}
    original_body = preview(client, original, original_body)
    assert client.post(f"/api/documents/{original['id']}/confirm", json=original_body, headers=HEAD).status_code == 200
    updated_body = preview(client, doc, body)
    c = client.post(f"/api/documents/{doc['id']}/confirm", json=updated_body, headers=HEAD)
    assert c.status_code == 200, c.text
    assert c.json()['payload']['due'] == '120.00'
    assert set(c.json()['payload']['documents']) == {original['id'], doc['id']}
