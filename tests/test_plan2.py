import json
import httpx
from test_cloud import cloud as cloud_fixture
from app import cloud_cascade
from app.providers import CloudProvider
from app.recognition_workflow import PLAN2
cloud=cloud_fixture


def chat(text):
    return httpx.Response(200,json={'choices':[{'message':{'content':text},'finish_reason':'stop'}]})


def test_plan2_always_runs_text_inference_after_all_ocr(cloud,tmp_path):
    page,config=cloud
    original=config['models']['primary']
    config.update(workflow=PLAN2,models={'primary':{**original,'model_id':'ocr','response_format':'ocr_text'},'secondary':{**original,'model_id':'27b'}})
    second={**page,'id':'second'};seen=[]
    def handler(request):
        body=json.loads(request.content);seen.append(body)
        if body['model']=='ocr':return chat('原始OCR：土地面积10平方米')
        content=body['messages'][0]['content']
        assert all(part['type']=='text' for part in content)
        assert len([p for p in content if 'untrusted_ocr_text' in p['text']])==2
        return chat(json.dumps({'fields':[{'field':'area','value':'10','raw_text':'土地面积10平方米','bbox':[.1,.1,.8,.8],'page':1}],'warnings':[]}))
    transport=httpx.MockTransport(handler)
    first=cloud_cascade.run_cloud_cascade([page,second],tmp_path/'plan2',config=config,transport=transport,defer_fallback=True)
    assert first['fallback_pending'] and len(seen)==2
    final=cloud_cascade.run_cloud_cascade([page,second],tmp_path/'plan2',config=config,transport=transport,primary_result=first)
    assert [b['model'] for b in seen]==['ocr','ocr','27b']
    assert final['completion_status']=='completed' and final['external_calls']==0
    assert final['candidates']['area'][0]['model']=='27b'
    assert final['candidates']['area'][0]['bbox'] is None


def test_failed_ocr_does_not_call_qwen(cloud,tmp_path):
    page,config=cloud;p=config['models']['primary'];config.update(workflow=PLAN2,models={'primary':p,'secondary':{**p,'model_id':'27b'}})
    seen=[]
    def handle(request):seen.append(1);return httpx.Response(500)
    out=cloud_cascade.run_cloud_cascade([page],tmp_path/'fail',config=config,transport=httpx.MockTransport(handle))
    assert len(seen)==1 and out['completion_status']=='failed' and out['recoverable']
