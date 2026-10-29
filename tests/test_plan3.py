import json
import httpx
from test_cloud import cloud as cloud_fixture
from app.cloud_cascade import run_cloud_cascade
from app.recognition_workflow import configure,PLAN3
from app.model_settings import status,save,apply_selection
from test_api import client as client_fixture
from app import providers
cloud=cloud_fixture
client=client_fixture


def test_default_image_review_reads_both_sources_and_saves_both_versions(cloud,tmp_path):
    page,c=cloud;p=c['models']['primary']
    c.update(workflow=PLAN3,models={'primary':{**p,'model_id':'ocr','response_format':'ocr_text'},
             'secondary':{**p,'model_id':'27b','include_original_images':True,'save_text_revision':True}})
    calls=[]
    def handle(request):
        body=json.loads(request.content);calls.append(body)
        if body['model']=='ocr':text='面积10平方米'
        else:
            content=body['messages'][0]['content']
            assert sum(x['type']=='image_url' for x in content)==1
            assert sum('untrusted_ocr_text' in x.get('text','') for x in content)==1
            text=json.dumps({'fields':[{'field':'area','value':'20','raw_text':'面积20平方米','page':1,'bbox':[.1,.1,.8,.8]}],
                             'transcriptions':[{'page':1,'text':'面积20平方米'}],'warnings':[]})
        return httpx.Response(200,json={'choices':[{'message':{'content':text},'finish_reason':'stop'}]})
    r=run_cloud_cascade([page],tmp_path/'image-review',config=c,transport=httpx.MockTransport(handle))
    assert len(calls)==2 and r['strategy']==PLAN3 and r['completion_status']=='completed'
    assert r['candidates']['area'][0]['bbox'] is not None
    revision=r['text_inference']['transcriptions'][0]
    assert revision['input_source']=='original_images_and_ocr'
    assert r['text_comparison']['pages'][0]['status']=='changed'
    assert '结合原图' in r['text_comparison']['basis']
    assert r['pages'][0]['stages'][0]['transcription']['text']=='面积10平方米'
    assert r['pages'][0]['stages'][1]['transcription']['text']=='面积20平方米'


def test_plan3_admin_selection_persists_without_changing_scope(client,monkeypatch,tmp_path):
    p=tmp_path/'providers.json';p.write_text(json.dumps({'enabled':False,'authorized_scope':'explicit_sample_hash',
       'models':{'primary':{'provider':'aliyun','authorized':True,'base_url':'https://example.invalid/v1','api_key_env':'FDE_TEST_KEY'}}}))
    monkeypatch.setenv('FDE_PROVIDERS_CONFIG',str(p));headers={'X-FDE-Role':'admin','X-FDE-Actor':'test'}
    before=client.get('/api/model-settings',headers=headers).json()
    body={'version':before['version'],'base_hash':before['base_hash'],'workflow_primary':'plan3','enabled':True}
    assert client.post('/api/model-settings',json=body).status_code==403
    response=client.post('/api/model-settings',json=body,headers=headers)
    assert response.status_code==200 and response.json()['workflow_primary']=='plan3'
    config=providers.configuration()
    assert config['workflow']==PLAN3 and config['models']['secondary']['include_original_images']
    assert config['authorized_scope']=='explicit_sample_hash'
    assert client.post('/api/model-settings',json=body,headers=headers).status_code==409


def test_plan3_base_config_is_reported_as_plan3(client,monkeypatch,tmp_path):
    base={'enabled':True,'models':{'primary':{'provider':'aliyun','authorized':True,'base_url':'https://example.invalid/v1'}}}
    p=tmp_path/'plan3.json';p.write_text(json.dumps(configure(base,'plan3')))
    monkeypatch.setenv('FDE_PROVIDERS_CONFIG',str(p))
    r=client.get('/api/model-settings',headers={'X-FDE-Role':'admin','X-FDE-Actor':'test'})
    assert r.json()['workflow_primary']=='plan3' and r.json()['selection'] is None
