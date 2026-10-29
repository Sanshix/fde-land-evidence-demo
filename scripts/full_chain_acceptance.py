"""Isolated acceptance: real scan uploads, answer-key review fixtures, real API writes.

This does not establish OCR accuracy or human review accuracy. Never uses the live DB.
"""
import os
import sys
import json
import io
import zipfile
from pathlib import Path
from decimal import Decimal

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/automated-20261029'
os.environ['FDE_DATA_DIR'] = str(RUN / ('chain-data-' + str(__import__('time').time_ns())))
os.environ['FDE_DISABLE_WORKER'] = '1'
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from fastapi.testclient import TestClient
from app import business, store, governance
from app.main import app
import exercise_pipeline

HEAD = {'X-FDE-Role': 'reviewer', 'X-FDE-Actor': 'automated-review-fixture'}
checks = []
def check(name, ok, detail=None):
    checks.append({'name': name, 'passed': bool(ok), 'detail': detail})
    print(json.dumps(checks[-1], ensure_ascii=False), flush=True)

def post(c, path, body, expected=200):
    r = c.post(path, json=body, headers=HEAD)
    check(path, r.status_code == expected, {'status': r.status_code, 'expected': expected, 'body': r.text[:1200]} if r.status_code != 200 else None)
    return r.json() if r.status_code == 200 else None

with TestClient(app) as c:
    docs = exercise_pipeline.truth_documents(business.SAMPLES / '08_标准答案')
    check('isolated_empty_database', not c.get('/api/status').json()['documents'])
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted((business.SAMPLES / '01_合同扫描件').glob('*.jpg')):
            z.write(p, p.name)
    r = c.post('/api/import-batch', files={'file': ('all-scans.zip', archive.getvalue(), 'application/zip')}, headers=HEAD)
    check('all_scans_zip_upload', r.status_code == 200 and not r.json().get('partial_failure'), r.json())
    imported = {d['name']: d for d in c.get('/api/status').json()['documents']}
    check('all_50_documents_imported', len(imported) == len(docs), {'expected': len(docs), 'actual': len(imported)})
    # Group-aware matching uses the same oracle assumptions as exercise_pipeline.
    groups = exercise_pipeline.group(docs, business.sources())
    group_fields = {d['id']: [x['fields'] for x in g] for g in groups for d in g}
    for item in docs:
        doc = c.get('/api/documents/' + imported[item['name']]['id']).json()
        f = item['fields']
        matches = business.suggest(f, business.sources(), group_fields[item['id']])
        body = {'version': doc['version'], 'fields': f,
                'citations': {k: doc['pages'][0]['id'] for k in f},
                'payment_ids': [str(x['序号']) for x in matches['payments']],
                'relief_ids': [x['批准文号'] for x in matches['reliefs']],
                'reason': 'AUTOMATED ORACLE FIXTURE: not real human review; first-page citations are test bindings only',
                'evidence_clear': True, 'material_complete': True,
                'confirm_calculation': True, 'confirm_payments': True}
        if f.get('altered_field'):
            body['alteration_review'] = {'decision': 'present' if f.get('altered_sealed') == '是' else 'absent', 'unit_id': doc['pages'][0]['id']}
        body['quality_resolutions'] = {governance.issue_id(x): {'decision': 'verified_with_evidence', 'reason': 'automated fixture only', 'source_unit': doc['pages'][0]['id']} for x in doc.get('intake', {}).get('issues', []) if x.get('severity') == 'blocking'}
        p = post(c, f"/api/documents/{doc['id']}/preview", body)
        check(item['name'] + '_preview_validation', p is not None and not p.get('errors'), p.get('errors') if p else None)
        if p and not p.get('errors'):
            post(c, f"/api/documents/{doc['id']}/confirm", body | {'draft_hash': p['draft_hash']})
    ledgers = c.get('/api/ledger').json()
    check('all_50_documents_published', all(d['state'] == 'published' for d in c.get('/api/status').json()['documents']))
    check('parcel_count', len(ledgers) == len(groups), {'actual': len(ledgers), 'expected': len(groups)})
    stats = c.get('/api/stats').json()
    check('stats_due_reconciles', Decimal(str(stats['累计应缴'])) == sum((Decimal(x['payload']['due']) for x in ledgers), Decimal(0)), stats)
    check('stats_paid_reconciles', Decimal(str(stats['累计实缴'])) == sum((Decimal(x['payload']['paid']) for x in ledgers), Decimal(0)))
    for ledger in ledgers:
        n = post(c, '/api/notices/' + str(ledger['id']), {}, expected=200 if Decimal(ledger['payload']['arrears']) > 0 else 400)
        if n:
            d = c.get('/api/notices/' + str(n['id']) + '/document')
            check('notice_docx_' + str(ledger['id']), d.status_code == 200 and zipfile.is_zipfile(io.BytesIO(d.content)))
            post(c, '/api/notices/' + str(n['id']) + '/events', {'status': '已审核', 'note': 'automated fixture'})
            post(c, '/api/notices/' + str(n['id']) + '/events', {'status': '已发送', 'note': 'manual status tracking fixture; no actual SMS'})
        draft = post(c, '/api/contracts/draft', {'parcel_id': ledger['parcel_id'], 'kind': '续期', 'start': '2027-01-01', 'end': '2036-12-31'})
        if draft:
            d = c.get('/api/contracts/' + str(draft['id']) + '/document')
            check('renewal_docx_' + str(ledger['id']), d.status_code == 200 and zipfile.is_zipfile(io.BytesIO(d.content)))
    for quarter in range(1, 5):
        p = post(c, '/api/tax-preview', {'year': 2026, 'quarter': quarter}, expected=200 if quarter <= 3 else 400)
        if p:
            tax = post(c, '/api/tax-push', {'year': 2026, 'quarter': quarter, 'preview_hash': p['preview_hash']})
            if tax:
                r = c.get('/api/tax-push/' + str(tax['id']) + '/document')
                check('tax_xlsx_Q' + str(quarter), r.status_code == 200 and zipfile.is_zipfile(io.BytesIO(r.content)))
    for path in ['/api/reminders', '/api/reminders?format=ics', '/api/stats?format=csv', '/api/governance', '/api/contracts', '/api/tax-pushes']:
        r = c.get(path)
        check(path, r.status_code == 200)
    r = c.get('/api/export')
    (RUN / 'all-data-export.zip').write_bytes(r.content)
    check('all_data_export', r.status_code == 200 and zipfile.is_zipfile(io.BytesIO(r.content)))
    backup = c.post('/api/backup', json={}, headers=HEAD | {'X-FDE-Role': 'admin'})
    check('backup', backup.status_code == 200, backup.json())
    if backup.status_code == 200:
        (RUN / 'backup.json').write_text(json.dumps(backup.json(), ensure_ascii=False, indent=2))
    (RUN / 'chain-results.json').write_text(json.dumps({'checks': checks, 'passed': sum(x['passed'] for x in checks), 'failed': sum(not x['passed'] for x in checks), 'ledgers': len(ledgers), 'stats': stats, 'oracle_review': True}, ensure_ascii=False, indent=2))
