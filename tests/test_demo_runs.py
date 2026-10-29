import hashlib
import io
import json
import time

import pytest
from PIL import Image
from test_api import HEAD
from test_api import client as client_fixture

from app import business, demo_runs, main
from app.cloud_cascade import profile_signature
from app.domain import FIELDS

client = client_fixture


@pytest.fixture
def fixtures(tmp_path, monkeypatch):
    root = tmp_path / 'fixtures'
    root.mkdir()
    (root / '01_合同扫描件').mkdir()
    for n in business.REQUIRED_SOURCES:
        (root / n).write_bytes(b'synthetic source table placeholder; sources() patched below')
    source = {**main.SOURCE, 'registry': [], 'payments': [], 'reliefs': []}
    monkeypatch.setattr(business, 'sources', lambda samples=None: source)
    cache = tmp_path / 'cache'
    cache.mkdir()
    cfg = {'models': {'primary': {'provider': 'synthetic', 'model_id': 'fixture'}}}
    (cache / 'providers.json').write_text(json.dumps(cfg))
    documents = []
    for index, color in enumerate(['red', 'blue', 'green', 'yellow', 'white'], 1):
        name = f'LX-{index:03}'
        image = io.BytesIO()
        Image.new('RGB', (100, 120), color).save(image, format='JPEG')
        raw = image.getvalue()
        (root / '01_合同扫描件' / (name + '_1.jpg')).write_bytes(raw)
        documents.append({'name': name, 'files': [name + '_1.jpg']})
        folder = cache / 'recognition' / name
        folder.mkdir(parents=True)
        result = {'pages': [{'source_sha256': hashlib.sha256(raw).hexdigest(), 'stages': [
            {'status': 'completed', 'provider': 'synthetic', 'requested_model': 'fixture',
             'stage': 'primary', 'raw_response': {'text': '承租人：合成测试人\n土地面积：2㎡'},
             'response_id': name + '-fixture'}]}]}
        (folder / 'result.json').write_text(json.dumps(result))
    (cache / 'documents.json').write_text(json.dumps(documents))
    configs = {'datasets': {key: {'label': key, 'directory': str(root), 'directory_env': 'SYNTHETIC_DEMO_DIR',
               'cache': str(cache), 'cache_profile': profile_signature(cfg), 'prefix': 'LX', 'simulation_only': True}
               for key in ['practice', 'evaluation']}}
    path = tmp_path / 'datasets.json'
    path.write_text(json.dumps(configs))
    monkeypatch.setenv('FDE_DEMO_CONFIG', str(path))
    return root, cache


def wait(client, identifier):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = client.get('/api/demo/status').json()
        run = next(x for x in status['runs'] if x['id'] == identifier)
        if run['state'] not in demo_runs.ACTIVE:
            return run
        time.sleep(0.02)
    raise AssertionError('Demo did not reach a durable terminal state')


def start(client, dataset='evaluation', mode='replay'):
    result = client.post('/api/demo/start', json={'dataset': dataset, 'mode': mode}, headers=HEAD)
    assert result.status_code == 200, result.text
    return result.json()['run']['id']


def test_cache_real_import_visible_report_idempotence_and_recoverable_reset(client, fixtures):
    formal_counts = client.get('/api/status').json()['documents']
    identifier = start(client)
    assert wait(client, identifier)['state'] == 'completed'
    assert len(client.get('/api/status').json()['documents']) == 5
    assert client.get('/api/status').json()['test_space']['id'] == identifier
    report = client.get(f'/api/demo/{identifier}/report').json()
    assert report['actual']['documents_imported'] == report['actual']['pages_imported'] == 5
    assert report['actual']['recognition_states'] == {'completed': 5}
    assert not report['scoring']['available'] and report['scoring']['reason'].startswith('评测集')
    assert not report['new_ocr'] and report['usage']['new_prompt_tokens'] == 0
    assert (demo_runs.folder(identifier) / 'business-sources' / business.REQUIRED_SOURCES[0]).is_file()
    assert start(client) == identifier
    plan = client.get(f'/api/demo/{identifier}/reset-plan').json()
    assert plan['counts']['documents'] == 5
    assert client.post(f'/api/demo/{identifier}/reset', json={'token': plan['token']}, headers=HEAD).status_code == 400
    result = client.post(f'/api/demo/{identifier}/reset', json={'token': plan['token'], 'confirm': True}, headers=HEAD)
    assert result.status_code == 200
    assert client.get('/api/status').json()['documents'] == formal_counts
    assert demo_runs.get(identifier)['archived']
    assert client.get(f'/api/demo/{identifier}/report').status_code == 200
    assert client.post(f'/api/demo/{identifier}/restore', json={}, headers=HEAD).status_code == 200
    assert len(client.get('/api/status').json()['documents']) == 5
    assert client.get('/api/ledger').json() == []
    assert client.post('/api/import', headers=HEAD).status_code == 409
    assert client.post('/api/documents/synthetic/retry', json={}, headers=HEAD).status_code == 409


def test_missing_or_mismatched_cache_fails_without_paid_fallback(client, fixtures, monkeypatch):
    _, cache = fixtures
    (cache / 'recognition/LX-002/result.json').unlink()
    path = cache / 'recognition/LX-003/result.json'
    bad = json.loads(path.read_text())
    bad['pages'][0]['source_sha256'] = 'bad'
    path.write_text(json.dumps(bad))
    monkeypatch.setattr(main, 'run_cloud_cascade', lambda *a, **k: pytest.fail('No live fallback allowed'))
    identifier = start(client)
    assert wait(client, identifier)['state'] == 'partial'
    report = client.get(f'/api/demo/{identifier}/report').json()
    assert report['actual']['recognition_states'] == {'completed': 3, 'failed': 2}
    assert all(not d['error'] or '缓存' in d['error'] for d in report['documents'])


def test_running_reset_rejected_role_source_errors_and_unknown_restart_visible(client, fixtures, monkeypatch):
    monkeypatch.setenv('FDE_DEMO_BATCH_PAUSE', '1')
    identifier = start(client)
    assert client.get(f'/api/demo/{identifier}/reset-plan').status_code == 409
    assert client.post('/api/demo/start', json={'dataset': 'practice', 'mode': 'replay'}, headers=HEAD).status_code == 400
    assert client.post('/api/demo/start', json={'dataset': 'practice', 'mode': 'live'}).status_code == 403
    wait(client, identifier)
    client.post('/api/demo/formal', json={}, headers=HEAD)
    monkeypatch.setenv('SYNTHETIC_DEMO_DIR', '/nonexistent/synthetic-demo-dir')
    response = client.post('/api/demo/start', json={'dataset': 'evaluation', 'mode': 'replay'}, headers=HEAD)
    assert response.status_code == 400 and '目录' in response.text


def test_practice_fixed_denominator_and_no_answers_in_extraction(client, fixtures):
    import csv

    from scripts.score_submission import FIELDS as SCORED
    root, _ = fixtures
    answers = root / '08_标准答案'
    answers.mkdir()
    with (answers / '合同要素.csv').open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['档案号', *SCORED])
        writer.writeheader()
        for i in range(1, 6):
            row = {key: '' for key in SCORED}
            row.update({'档案号': f'LX-{i:03}', '承租人': '合成测试人', '土地面积': '2'})
            writer.writerow(row)
    identifier = start(client, 'practice')
    assert wait(client, identifier)['state'] == 'completed'
    report = client.get(f'/api/demo/{identifier}/report').json()
    assert report['scoring']['strict']['correct'] == report['scoring']['strict']['denominator'] == 10
    assert report['scoring']['score_100'] == 100 and report['scoring']['maximum'] == 100
    assert report['actual']['published'] == 0


def test_live_adapter_contract_uses_config_and_separate_workspace(client, fixtures, monkeypatch):
    calls = []
    config = {'enabled': True, 'authorized_scope': 'competition_simulated', 'models': {}}
    monkeypatch.setattr(main, 'configuration', lambda: config)
    def adapter(pages, run_dir, strategy, **kwargs):
        calls.append(kwargs['config'])
        return {'candidates': {k: [] for k in FIELDS}, 'pages': [], 'completion_status': 'failed',
                'elapsed_seconds': 0, 'mode': 'test_stub', 'risks': [], 'external_calls': 0}
    monkeypatch.setattr(main, 'run_cloud_cascade', adapter)
    identifier = start(client, mode='live')
    assert wait(client, identifier)['state'] == 'partial'
    assert len(calls) == 5 and all(c == config for c in calls)
    report = client.get(f'/api/demo/{identifier}/report').json()
    assert report['mode'] == 'live' and not report['new_ocr'] and report['external_calls_new'] == 0
    assert report['actual']['recognition_states'] == {'failed': 5}
    client.post('/api/demo/formal', json={}, headers=HEAD)
    assert client.get('/api/status').json()['documents'] == []


def test_stale_reset_token_cross_dataset_boundary_and_invalid_id(client, fixtures):
    from app import store
    first = start(client)
    wait(client, first)
    plan = client.get(f'/api/demo/{first}/reset-plan').json()
    with store.use_workspace(demo_runs.workspace(demo_runs.get(first))), store.connection() as db:
        store.audit(db, 'reviewer', 'synthetic_change', first, {})
    assert client.post(f'/api/demo/{first}/reset', json={'token': plan['token'], 'confirm': True}, headers=HEAD).status_code == 409
    second = start(client, 'practice')
    wait(client, second)
    plan = client.get(f'/api/demo/{first}/reset-plan').json()
    assert client.post(f'/api/demo/{first}/reset', json={'token': plan['token'], 'confirm': True}, headers=HEAD).status_code == 200
    assert len(client.get('/api/status').json()['documents']) == 5
    assert not demo_runs.get(second)['archived']
    assert client.post('/api/demo/invalid/reset', json={'confirm': True}, headers=HEAD).status_code == 400
    assert client.post('/api/demo/invalid/restore', json={}, headers=HEAD).status_code == 400


def test_orphan_recovery_terminal_unknown_without_resubmitting(client, fixtures, monkeypatch):
    from app import store
    identifier = start(client)
    wait(client, identifier)
    # Model request was in progress when a previous process died.
    run = demo_runs.get(identifier)
    with store.use_workspace(demo_runs.workspace(run)), store.connection() as db:
        db.execute("UPDATE jobs SET state='running'")
    run['created'] = time.time() - 30
    demo_runs.save(run, state='running')
    monkeypatch.setattr(main, 'run_cloud_cascade', lambda *a, **k: pytest.fail('Unknown work must not be resubmitted'))
    demo_runs.recover()
    assert demo_runs.get(identifier)['state'] == 'interrupted'
    report = client.get(f'/api/demo/{identifier}/report').json()
    assert report['actual']['recognition_states'] == {'unknown': 5}
    assert not report['new_ocr']


def test_live_unknown_not_retried_and_no_send_in_test_space(client, fixtures, monkeypatch):
    config = {'enabled': True, 'authorized_scope': 'competition_simulated', 'models': {}}
    monkeypatch.setattr(main, 'configuration', lambda: config)
    calls = []
    def adapter(*args, **kwargs):
        calls.append(1)
        return {'candidates': {k: [] for k in FIELDS}, 'pages': [], 'completion_status': 'unknown',
                'elapsed_seconds': 0, 'mode': 'test_stub', 'risks': [], 'external_calls': 1}
    monkeypatch.setattr(main, 'run_cloud_cascade', adapter)
    identifier = start(client, mode='live')
    wait(client, identifier)
    assert start(client, mode='live') == identifier and len(calls) == 5
    assert client.post('/api/notices/synthetic/send', json={}, headers=HEAD).status_code == 403
    assert client.get(f'/api/demo/{identifier}/report').json()['external_calls_new'] == 5
