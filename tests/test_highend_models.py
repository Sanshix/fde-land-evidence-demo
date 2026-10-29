"""Commercial models retain the image/text workflow and authorization boundaries."""
import json

import pytest
import httpx

from app import providers
from app.recognition_workflow import configure, HIGH_END, PLAN3
from test_api import client as client_fixture
from test_cloud import cloud as cloud_fixture

client = client_fixture
cloud = cloud_fixture


@pytest.mark.parametrize('choice,model', HIGH_END.items())
def test_highend_selection_persistence_permissions_and_scope(client, monkeypatch, tmp_path, choice, model):
    base = {'enabled': False, 'authorized_scope': 'explicit_sample_hash', 'models': {'primary': {
        'provider': 'aliyun', 'authorized': True, 'base_url': 'https://example.invalid/v1',
        'api_key_env': 'FDE_TEST_KEY'}}}
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps(base))
    monkeypatch.setenv('FDE_PROVIDERS_CONFIG', str(path))
    admin = {'X-FDE-Role': 'admin', 'X-FDE-Actor': 'test-highend'}
    before = client.get('/api/model-settings', headers=admin).json()
    body = {'version': before['version'], 'base_hash': before['base_hash'],
            'workflow_primary': choice, 'enabled': False}
    assert client.post('/api/model-settings', json=body).status_code == 403
    response = client.post('/api/model-settings', json=body, headers=admin)
    assert response.status_code == 200, response.text
    assert response.json()['workflow_primary'] == choice
    active = providers.configuration()
    assert active['workflow'] == PLAN3 and active['enabled'] is False
    assert active['authorized_scope'] == 'explicit_sample_hash'
    assert active['models']['primary']['response_format'] == 'ocr_text'
    profile = active['models']['secondary']
    assert profile['model_id'] == model and profile['open_weights'] is False
    assert profile['license'] == 'commercial_api'
    assert profile['include_original_images'] and profile['save_text_revision']
    assert profile['thinking_budget'] == 1024 and profile['max_tokens'] == 12000
    assert client.post('/api/model-settings', json=body, headers=admin).status_code == 409
    # A deployment file using the same profile is described correctly without saved selection.
    path.write_text(json.dumps(configure(base, choice)))
    status = client.get('/api/model-settings', headers=admin).json()
    assert status['workflow_primary'] == choice and status['base_changed']


def test_highend_never_creates_authorization():
    with pytest.raises(ValueError, match='授权'):
        configure({'models': {}}, 'plan3-qwen3.7-plus')
    with pytest.raises(ValueError):
        configure({'models': {}}, 'plan3-unknown')


@pytest.mark.parametrize('choice,model', HIGH_END.items())
def test_highend_two_stage_protocol_and_material_scope(cloud, tmp_path, choice, model):
    from app.cloud_cascade import run_cloud_cascade
    page, base = cloud
    base['models']['primary']['provider'] = 'aliyun'
    config = configure(base, choice)
    calls = []

    def handle(request):
        body = json.loads(request.content)
        calls.append(body)
        if body['model'] == 'qwen-vl-ocr-2025-11-20':
            return httpx.Response(200, json={'choices': [{'message': {'content': '面积10平方米'},
                                                        'finish_reason': 'stop'}]})
        assert body['model'] == model
        assert body['enable_thinking'] and body['thinking_budget'] == 1024
        assert body['max_tokens'] == 12000
        content = body['messages'][0]['content']
        assert any(x['type'] == 'image_url' for x in content)
        assert any('untrusted_ocr_text' in x.get('text', '') for x in content)
        text = json.dumps({'fields': [{'field': 'area', 'value': '10', 'raw_text': '面积10平方米',
                                     'page': 1, 'bbox': None}],
                           'transcriptions': [{'page': 1, 'text': '面积10平方米'}]})
        chunk = {'model': model, 'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}],
                 'usage': {'total_tokens': 100}}
        return httpx.Response(200, text='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')

    transport = httpx.MockTransport(handle)
    result = run_cloud_cascade([page], tmp_path / 'permitted', config=config, transport=transport)
    assert len(calls) == 2 and result['completion_status'] == 'completed'
    assert result['text_inference']['requested_model'] == model
    assert result['candidates']['area'][0]['value'] == '10'
    revision = result['text_inference']['transcriptions'][0]
    assert revision['model'] == model and revision['input_source'] == 'original_images_and_ocr'
    calls.clear()
    config['authorized_sha256'] = []
    result = run_cloud_cascade([page], tmp_path / 'denied', config=config, transport=transport)
    assert calls == [] and result['external_calls'] == 0
