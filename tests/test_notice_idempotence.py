import json
from concurrent.futures import ThreadPoolExecutor

from test_api import HEAD, client, new_doc, preview, reviewed  # noqa: F401

from app import store


def published(web):
    doc, _ = new_doc(web)
    body = preview(web, doc, reviewed(doc))
    response = web.post(f"/api/documents/{doc['id']}/confirm", json=body, headers=HEAD)
    assert response.status_code == 200
    return response.json()


def test_repeated_notice_creation_reuses_same_immutable_draft(client):  # noqa: F811
    pub = published(client)
    url = f"/api/notices/{pub['id']}"
    first = client.post(url, json={}, headers=HEAD)
    second = client.post(url, json={}, headers=HEAD)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM notices').fetchone()[0] == 1


def test_changed_notice_settings_create_new_preserved_draft(client, tmp_path, monkeypatch):  # noqa: F811
    config = tmp_path / 'notice-config.json'
    monkeypatch.setenv('FDE_NOTICE_CONFIG', str(config))
    pub = published(client)
    url = f"/api/notices/{pub['id']}"
    first = client.post(url, json={}, headers=HEAD).json()
    config.write_text(json.dumps({'期限天数': 45}))
    second = client.post(url, json={}, headers=HEAD).json()
    assert first['id'] != second['id']
    with store.connection() as db:
        saved = json.loads(db.execute('SELECT payload FROM notices WHERE id=?', (first['id'],)).fetchone()[0])
    assert saved == first
    assert client.post(url, json={}, headers=HEAD).json() == second


def test_concurrent_notice_creation_is_serialized(client):  # noqa: F811
    pub = published(client)
    url = f"/api/notices/{pub['id']}"
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post(url, json={}, headers=HEAD), range(2)))
    assert all(r.status_code == 200 for r in responses)
    assert len({r.json()['id'] for r in responses}) == 1
    with store.connection() as db:
        assert db.execute('SELECT COUNT(*) FROM notices').fetchone()[0] == 1


def test_changed_template_creates_new_draft_and_keeps_old_file(client, tmp_path, monkeypatch):  # noqa: F811
    from app import outputs

    original_path = outputs.template('国有土地租金催缴通知书（模板）.docx')
    active = tmp_path / 'notice-template.docx'
    active.write_bytes(original_path.read_bytes())
    monkeypatch.setattr(outputs, 'template', lambda _name: active)
    pub = published(client)
    url = f"/api/notices/{pub['id']}"
    first = client.post(url, json={}, headers=HEAD).json()
    import zipfile

    with zipfile.ZipFile(active, 'a') as archive:
        archive.comment = b'version-two'
    second = client.post(url, json={}, headers=HEAD).json()
    assert second['id'] != first['id']
    assert (store.DATA / 'objects' / (first['docx_sha256'] + '.docx')).is_file()
    assert client.post(url, json={}, headers=HEAD).json() == second
