import json
import httpx
import pytest
from test_api import client as client_fixture
from app import providers, store
from app.cloud_cascade import profile_signature
client=client_fixture
ADMIN={'X-FDE-Role':'admin','X-FDE-Actor':'model-test'}
def load(c): return c.get('/api/model-settings',headers=ADMIN).json()
def body(s): return dict(version=s['version'],base_hash=s['base_hash'],model=s['models'][0]['id'],deployment='local',endpoint='http://127.0.0.1:8000/v1',enabled=True)
def test_permissions_persistence_reset_conflict(client):
    assert client.get('/api/model-settings').status_code==403
    data=body(load(client))
    assert client.post('/api/model-settings',json=data).status_code==403
    r=client.post('/api/model-settings',json=data,headers=ADMIN)
    assert r.status_code==200,r.text
    config=providers.configuration()
    assert config['enabled'] and set(config['models'])=={'primary'}
    assert config['models']['primary']['enable_thinking']
    assert load(client)['selection']['profile']['model_id']=='Qwen/Qwen3.8-27B'
    assert client.post('/api/model-settings',json=data,headers=ADMIN).status_code==409
    s=load(client)
    assert client.post('/api/model-settings',json=dict(version=s['version'],base_hash=s['base_hash'],reset=True),headers=ADMIN).status_code==200
    assert providers.configuration()==providers.base_configuration()
    assert load(client)['base_changed'] is False
    with store.connection() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='model_selection'").fetchone()[0]==2
@pytest.mark.parametrize('endpoint',['https://example.com/v1','http://user:pass@127.0.0.1/v1','http://169.254.169.254/v1','http://0.0.0.0/v1','http://127.0.0.1/v1?key=x'])
def test_reject_nonlocal(client,endpoint):
    data=body(load(client));data['endpoint']=endpoint
    assert client.post('/api/model-settings',json=data,headers=ADMIN).status_code==400

def test_base_change(client,monkeypatch,tmp_path):
    assert client.post('/api/model-settings',json=body(load(client)),headers=ADMIN).status_code==200
    path=tmp_path/'providers.json';path.write_text('{"enabled":false,"models":{},"reason":"changed"}')
    monkeypatch.setenv('FDE_PROVIDERS_CONFIG',str(path))
    assert providers.configuration()['enabled'] is False
    assert load(client)['base_changed']

def test_cloud_scope(client,monkeypatch,tmp_path):
    path=tmp_path/'providers.json';path.write_text(json.dumps({'enabled':False,'authorized_scope':'explicit_sample_hash','models':{'primary':{'provider':'aliyun','authorized':True,'base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1','api_key_env':'AI_ALIYUN_KEY','response_format':'ocr_text'}}}))
    monkeypatch.setenv('FDE_PROVIDERS_CONFIG',str(path))
    data=body(load(client));data.update(deployment='aliyun',enabled=False)
    assert client.post('/api/model-settings',json=data,headers=ADMIN).status_code==200
    config=providers.configuration()
    assert config['authorized_scope']=='explicit_sample_hash' and config['enabled'] is False
    assert 'response_format' not in config['models']['primary']
    assert config['models']['primary']['model_id']=='qwen3.8-27b'

@pytest.mark.parametrize('finish',['stop','length',None])
def test_stream(finish):
    frames=[{'choices':[{'delta':{'reasoning_content':'not evidence'}}]}, {'choices':[{'delta':{'content':'{"fields":[]}'}}]}]
    if finish: frames.append({'choices':[{'delta':{},'finish_reason':finish}]})
    frames.append({'choices':[],'usage':{'completion_tokens':20},'model':'served-model'})
    raw=''.join('data: '+json.dumps(f)+'\n\n' for f in frames)+'data: [DONE]\n\n'
    def handle(request):
        data=json.loads(request.content)
        assert data['stream'] and data['chat_template_kwargs']['enable_thinking']
        return httpx.Response(200,text=raw)
    p=providers.CloudProvider({'provider':'local','base_url':'http://127.0.0.1:8000/v1','model_id':'local','stream':True,'enable_thinking':True},httpx.MockTransport(handle))
    result=p.complete({'model':'local'},10)
    if finish=='stop':
        raw,e=providers.response_content(result)
        assert providers.response_json(raw,e)=={'fields':[]}
        assert result['usage']['completion_tokens']==20
    else:
        with pytest.raises(providers.ProviderResponseError): providers.response_content(result)

def test_cache_signature():
    assert profile_signature({'models':{'primary':{'enable_thinking':True}}})!=profile_signature({'models':{'primary':{'enable_thinking':False}}})

@pytest.mark.parametrize('thinking_only',[False,True])
def test_aliyun_stream_parameters(monkeypatch,thinking_only):
    monkeypatch.setattr(providers,'auth_headers',lambda p:{})
    def handle(request):
        data=json.loads(request.content)
        assert data['thinking_budget']==4096 and data['max_tokens']==12000
        assert 'chat_template_kwargs' not in data
        assert ('enable_thinking' in data) is (not thinking_only)
        return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
    profile={'provider':'aliyun','base_url':'https://dashscope.aliyuncs.com/compatible-mode/v1','model_id':'qwen','stream':True,'enable_thinking':True,'thinking_only':thinking_only}
    assert providers.CloudProvider(profile,httpx.MockTransport(handle)).complete({'model':'qwen'},10)['choices'][0]['finish_reason']=='stop'


def test_unavailable_cloud_model_keeps_previous_selection(client, monkeypatch, tmp_path):
    path = tmp_path / 'providers.json'
    path.write_text(json.dumps({'enabled': False, 'models': {'primary': {
        'provider': 'aliyun', 'authorized': True,
        'base_url': 'https://ws-gii3yuqz050k1sww.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
        'api_key_env': 'AI_ALIYUN_KEY'}}}))
    monkeypatch.setenv('FDE_PROVIDERS_CONFIG', str(path))
    data = body(load(client))
    data.update(model='qwen3-vl-8b-thinking', deployment='aliyun')
    response = client.post('/api/model-settings', json=data, headers=ADMIN)
    assert response.status_code == 400
    assert '404' in response.text
    assert load(client)['selection'] is None
    data.update(deployment='local')
    assert client.post('/api/model-settings', json=data, headers=ADMIN).status_code == 200
    assert providers.configuration()['models']['primary']['model_id'] == 'Qwen/Qwen3-VL-8B-Thinking'
