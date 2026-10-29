import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from test_alignment import upload
from test_api import client as client_fixture

from app import main, store
from app.cloud_attempts import AttemptRunner, set_request_queue

client = client_fixture


def test_parallel_claim_and_no_age_cutoff(client, monkeypatch):
    docs = [upload(client, f'parallel-{i}', color) for i, color in enumerate(['white', 'black'])]
    barrier = threading.Barrier(2)
    calls = []
    def fake(*args, **kwargs):
        assert kwargs['budget'] == float('inf')
        calls.append(args[1].name)
        barrier.wait(timeout=3)
        return {'mode': 'test_stub', 'elapsed_seconds': .01, 'risks': [], 'fallback_pending': False}
    monkeypatch.setattr(main, 'run_cloud_cascade', fake)
    monkeypatch.setattr(main, 'configuration', lambda: {})
    with store.connection() as db:
        db.execute('UPDATE jobs SET created=?', (time.time()-3600,))
    with ThreadPoolExecutor(2) as pool:
        assert all(f.result() for f in [pool.submit(main.worker_once) for _ in range(2)])
    assert set(calls) == {d['id'] for d in docs}
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM jobs WHERE state='done'").fetchone()[0] == 2
        db.execute("UPDATE jobs SET state='queued'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO jobs(id,document_id,state,strategy,created) VALUES('duplicate',?,'queued','cascade',0)", (docs[0]['id'],))


def test_fallback_ignores_legacy_deadline(client, monkeypatch):
    doc = upload(client, 'legacy-deadline', 'white')
    def fake(*args, **kwargs):
        assert kwargs['budget'] == float('inf')
        return {'mode': 'test_stub', 'elapsed_seconds': .01, 'risks': [], 'fallback_pending': kwargs.get('defer_fallback', False)}
    monkeypatch.setattr(main, 'run_cloud_cascade', fake)
    monkeypatch.setattr(main, 'configuration', lambda: {})
    main.worker_once()
    import json
    path = next((store.DATA/'runs'/doc['id']).glob('*-continuation.json'))
    payload = json.loads(path.read_text())
    payload['deadline'] = 0
    path.write_text(json.dumps(payload))
    assert main.worker_once(fallback=True)
    with store.connection() as db:
        assert db.execute('SELECT state FROM jobs').fetchone()[0] == 'done'


def test_request_pools_allow_four_calls(tmp_path):
    barrier = threading.Barrier(4)
    def invoke(_):
        barrier.wait(timeout=3)
        return {'candidates': {}}
    def run(index):
        set_request_queue(index >= 2)
        return AttemptRunner(tmp_path/str(index), float('inf'), transport=True).call(str(index), 'test',
                {'provider': 'test', 'model_id': 'test'}, invoke, limit=5)
    with ThreadPoolExecutor(4) as pool:
        rows = list(pool.map(run, range(4)))
    assert all(r['status'] == 'completed' for r in rows)


def test_field_cleanup_preserves_real_ambiguity():
    from app.evidence import comparison_value
    from app.governance import normalize
    assert normalize('tenant', '(乙方):龚明淑') == ['龚明淑']
    assert normalize('identity', '990524196008035852 联系电话 10048046937') == ['990524196008035852']
    assert normalize('area', '158.04 m2') == ['158.04']
    assert normalize('area', '2平方米') == ['2']
    assert normalize('area', '569.46 579.46平方米') == ['569.46', '579.46']
    assert comparison_value('signed', '二〇〇二年六月三日 签订日期:二〇〇二年六月三日') == '2002-06-03'
    assert normalize('mode', '按年') == []
    assert normalize('doc_type', '国有土地使用权租赁合同') == ['新签']


def test_cancelled_queue_does_not_send(tmp_path):
    stop = threading.Event()
    stop.set()
    set_request_queue(False, stop)
    try:
        result = AttemptRunner(tmp_path, float('inf'), transport=True).call(
            'cancelled', 'test', {'provider': 'test', 'model_id': 'test'},
            lambda _: pytest.fail('cancelled request was sent'))
        assert result['status'] == 'interrupted'
        assert result['sent'] is False
    finally:
        set_request_queue(False)


def test_pool_wait_does_not_spend_request_timeout(tmp_path):
    entered = threading.Barrier(3)
    release = threading.Event()
    def occupy(_):
        entered.wait(timeout=3)
        assert release.wait(3)
        return {'candidates': {}}
    def call(index, invoke, limit):
        set_request_queue(False)
        return AttemptRunner(tmp_path/str(index), float('inf'), transport=True).call(
            str(index), 'test', {'provider': 'test', 'model_id': 'test'}, invoke, limit=limit)
    with ThreadPoolExecutor(3) as pool:
        occupied = [pool.submit(call, i, occupy, 2) for i in range(2)]
        entered.wait(timeout=3)
        queued = pool.submit(call, 3, lambda _: {'candidates': {}}, .05)
        time.sleep(.12)
        assert not queued.done()
        release.set()
        assert queued.result()['status'] == 'completed'
        assert all(f.result()['status'] == 'completed' for f in occupied)


def test_date_normalization_does_not_hide_a_second_date():
    from app.governance import normalize
    text = '2002-06-03 二〇〇三年六月三日'
    assert normalize('signed', text) == [text]
    assert normalize('signed', '2002-06-03 二〇〇二年六月三日') == ['2002-06-03']
