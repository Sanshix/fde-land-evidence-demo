"""Durable fallback work must not block later primary jobs or overwrite human edits."""
import threading

import pytest
from test_alignment import upload
from test_api import client as client_fixture

from app import main, store

client = client_fixture

@pytest.mark.parametrize('edit_state', [None, 'needs_human', 'held', 'withdrawn', 'published'])
def test_next_primary_runs_while_fallback_waits(client, monkeypatch, edit_state):
    first = upload(client, 'background-first', 'white')
    second = upload(client, 'foreground-next', 'black')
    entered, release = threading.Event(), threading.Event()
    calls = []
    def fake(pages, run_dir, strategy, **kwargs):
        phase = 'primary' if kwargs.get('defer_fallback') else 'fallback'
        calls.append((run_dir.name, phase))
        if phase == 'fallback':
            assert kwargs['primary_result']['marker'] == 'primary'
            entered.set()
            assert release.wait(3)
        return {'mode': 'test_stub', 'elapsed_seconds': .01, 'risks': [], 'pages': [],
                'candidates': {}, 'marker': phase, 'fallback_pending': phase == 'primary'}
    monkeypatch.setattr(main, 'run_cloud_cascade', fake)
    assert main.worker_once()
    assert store.get_doc(first['id'])['result']['marker'] == 'primary'
    with store.connection() as db:
        assert db.execute('SELECT state FROM jobs WHERE document_id=?', (first['id'],)).fetchone()[0] == 'fallback_queued'
    thread = threading.Thread(target=main.worker_once, kwargs={'fallback': True})
    thread.start()
    try:
        assert entered.wait(2)
        assert main.worker_once()  # Must finish while first document's fallback is blocked.
        assert store.get_doc(second['id'])['result']['marker'] == 'primary'
        assert calls[:3] == [(first['id'], 'primary'), (first['id'], 'fallback'), (second['id'], 'primary')]
        if edit_state:
            with store.connection() as db:
                db.execute('UPDATE documents SET version=version+1,state=? WHERE id=?', (edit_state, first['id']))
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive()
    final = store.get_doc(first['id'])
    assert final['result']['marker'] == ('primary' if edit_state else 'fallback')
    with store.connection() as db:
        assert db.execute('SELECT state FROM jobs WHERE document_id=?', (first['id'],)).fetchone()[0] == ('cancelled' if edit_state else 'done')


def test_queued_fallback_survives_restart_without_rerunning_primary(client, monkeypatch):
    doc = upload(client, 'restart-fallback', 'white')
    calls = []
    def fake(*args, **kwargs):
        phase = 'primary' if kwargs.get('defer_fallback') else 'fallback'
        calls.append(phase)
        return {'mode':'test_stub', 'elapsed_seconds':0, 'risks':[], 'fallback_pending':phase == 'primary'}
    monkeypatch.setattr(main, 'run_cloud_cascade', fake)
    main.worker_once()
    store.init()
    main.worker_once(fallback=True)
    assert calls == ['primary', 'fallback']
    with store.connection() as db:
        db.execute("UPDATE jobs SET state='fallback_running' WHERE document_id=?", (doc['id'],))
    store.init()
    with store.connection() as db:
        assert db.execute('SELECT state FROM jobs WHERE document_id=?', (doc['id'],)).fetchone()[0] == 'interrupted'


def test_pending_fallback_blocks_duplicate_retry_and_skips_edited_document(client, monkeypatch):
    from test_api import HEAD
    doc = upload(client, 'pending-fallback', 'white')
    calls = []
    def fake(*args, **kwargs):
        calls.append('primary')
        return {'mode':'test_stub', 'elapsed_seconds':0, 'risks':[], 'fallback_pending':True}
    monkeypatch.setattr(main, 'run_cloud_cascade', fake)
    main.worker_once()
    current = store.get_doc(doc['id'])
    response = client.post('/api/documents/' + doc['id'] + '/retry',
                           json={'version':current['version']}, headers=HEAD)
    assert response.status_code == 409
    with store.connection() as db:
        db.execute('UPDATE documents SET version=version+1 WHERE id=?', (doc['id'],))
    main.worker_once(fallback=True)
    assert calls == ['primary']
    with store.connection() as db:
        assert db.execute('SELECT state FROM jobs WHERE document_id=?', (doc['id'],)).fetchone()[0] == 'cancelled'
