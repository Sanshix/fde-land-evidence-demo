"""Routing acceptance with controlled evidence; no model accuracy claims."""
import json

import httpx
import pytest
from test_cloud import cloud as cloud_fixture
from test_api import client as client_fixture
from app import cloud_cascade, providers
from app.domain import FIELDS
from app.recognition_workflow import WORKFLOW, configure

cloud = cloud_fixture
client = client_fixture


def chat(fields, warnings=()):
    return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(
        {'fields': fields, 'warnings': list(warnings)})}, 'finish_reason': 'stop'}]})


def field(value='10'):
    return {'field': 'area', 'value': value, 'raw_text': '土地面积 '+value,
            'bbox': [.1,.1,.8,.4], 'reason': '核对图片面积栏'}


def setup(config, monkeypatch, risks):
    config['workflow'] = WORKFLOW
    primary = config['models']['primary']
    config['models'] = {'primary': primary, 'secondary': {**primary, 'model_id':'ocr-backup'},
                        'context': primary}
    monkeypatch.setattr(cloud_cascade, 'quality', lambda _: {'warnings': []})
    monkeypatch.setattr(cloud_cascade, 'assess', lambda *args: risks)


def test_single_path_does_not_trigger_backup(cloud, tmp_path, monkeypatch):
    page, config = cloud
    setup(config, monkeypatch, [{'field':'area','code':'single_path'}])
    seen=[]
    def handle(request):
        seen.append(json.loads(request.content)['model'])
        return chat([field()])
    result=cloud_cascade.run_cloud_cascade([page],tmp_path/'clean',config=config,
        transport=httpx.MockTransport(handle))
    assert seen==['stub-primary']
    assert not result['fallback_pending']


@pytest.mark.parametrize('trigger',['missing','disagreement','invalid_format','low_score','warning','cannot_determine'])
def test_uncertainty_uses_backup_then_qwen_review(cloud,tmp_path,monkeypatch,trigger):
    page,config=cloud
    risks=[] if trigger in {'warning','cannot_determine'} else [{'field':'area','code':trigger}]
    setup(config,monkeypatch,risks)
    seen=[]
    def handle(request):
        body=json.loads(request.content);seen.append(body)
        if len(seen)==1:
            return chat([{**field(),**({'status':'cannot_determine'} if trigger=='cannot_determine' else {})}],
                        ['涂改不清'] if trigger=='warning' else [])
        return chat([field('11')])
    transport=httpx.MockTransport(handle)
    first=cloud_cascade.run_cloud_cascade([page],tmp_path/trigger,config=config,
        transport=transport,defer_fallback=True)
    assert first['fallback_pending'] and len(seen)==1
    final=cloud_cascade.run_cloud_cascade([page],tmp_path/trigger,config=config,
        transport=transport,primary_result=first)
    assert [b['model'] for b in seen]==['stub-primary','ocr-backup','stub-primary']
    assert 'untrusted_previous_candidates' in seen[-1]['messages'][1]['content'][0]['text']
    assert {c['value'] for c in final['candidates']['area']} >= {'11'}
    suggestions=[c for c in final['candidates']['area'] if c['kind']=='context_suggestion']
    assert suggestions and not suggestions[0]['verified'] and not suggestions[0]['can_publish']


def test_presets_preserve_scope_and_swap_roles():
    base={'enabled':False,'authorized_scope':'explicit_sample_hash','authorized_sha256':['sample'],
          'models':{'primary':{'provider':'aliyun','authorized':True,
             'base_url':'https://example.invalid/v1','api_key_env':'FDE_TEST_KEY'}}}
    config=configure(base)
    assert config['models']['primary']['model_id']=='qwen3.5-27b'
    assert config['models']['secondary']['response_format']=='ocr_text'
    assert config['models']['context']['enable_thinking']
    assert config['authorized_sha256']==['sample'] and not config['enabled']
    reverse=configure(base,'qwen-vl-ocr-2025-11-20')
    assert reverse['models']['secondary']['model_id']=='qwen3.5-27b'
    assert base['models']['primary'].get('model_id') is None


def test_workflow_selection_persists_and_conflicts(client,monkeypatch,tmp_path):
    p=tmp_path/'config.json';p.write_text(json.dumps({'enabled':False,'authorized_scope':'explicit_sample_hash',
      'models':{'primary':{'provider':'aliyun','authorized':True,'base_url':'https://example.invalid/v1','api_key_env':'FDE_TEST_KEY'}}}))
    monkeypatch.setenv('FDE_PROVIDERS_CONFIG',str(p))
    headers={'X-FDE-Role':'admin','X-FDE-Actor':'workflow-test'}
    before=client.get('/api/model-settings',headers=headers).json()
    body={'version':before['version'],'base_hash':before['base_hash'],
          'workflow_primary':'qwen-vl-ocr-2025-11-20','enabled':True}
    assert client.post('/api/model-settings',json=body).status_code==403
    assert client.post('/api/model-settings',json=body,headers=headers).status_code==200
    assert client.post('/api/model-settings',json=body,headers=headers).status_code==409
    config=providers.configuration()
    assert config['models']['primary']['model_id']==body['workflow_primary']
    assert config['models']['secondary']['model_id']=='qwen3.5-27b'
    assert config['authorized_scope']=='explicit_sample_hash'


def test_fallback_budget_is_remaining_budget_not_charged_twice(cloud,tmp_path,monkeypatch):
    page,config=cloud
    setup(config,monkeypatch,[{'field':'area','code':'missing'}])
    first={'elapsed_seconds':80,'external_calls':1,'pages':[{'id':page['id'],'elapsed_seconds':80,
        'stages':[{'stage':'primary','status':'completed','candidates':{k:[] for k in FIELDS}}]}]}
    seen=[]
    def handle(request):
        seen.append(json.loads(request.content)['model']);return chat([field()])
    result=cloud_cascade.run_cloud_cascade([page],tmp_path/'remaining',budget=30,config=config,
        transport=httpx.MockTransport(handle),primary_result=first)
    assert seen==['ocr-backup','stub-primary']
    assert not result['budget_exhausted']
