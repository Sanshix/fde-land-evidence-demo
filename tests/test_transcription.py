import json
import httpx
import pytest
from test_cloud import cloud as cloud_fixture
from test_api import client as client_fixture, new_doc
from app import providers, transcription, store
from app.cloud_cascade import profile_signature

cloud=cloud_fixture
client=client_fixture
TEXT='国有土地租赁合同\n第五条 按时缴纳租金。\n| 项目 | 金额 |\n| --- | --- |\n| 租金 | 120.00 |\n[无法确定]\n<script>not executable</script>'


def reply(text):
    return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(text)},'finish_reason':'stop'}]})


def test_full_text_separate_from_fields_and_context(cloud):
    page,config=cloud;p={**config['models']['primary'],'full_transcription':True}
    def handle(request):
        prompt=json.loads(json.loads(request.content)['messages'][1]['content'][0]['text'])
        assert 'transcription' in prompt['output_schema']
        return reply({'fields':[],'transcription':TEXT})
    out=providers.CloudProvider(p,httpx.MockTransport(handle)).identify(page)
    assert out['transcription']['text']==TEXT
    assert out['transcription']['source_page_id']==page['id']
    assert not out['transcription']['verified']
    out=providers.CloudProvider(p,httpx.MockTransport(lambda _:reply({'fields':[],'transcription':'inferred rewrite'}))).identify(page,context=True)
    assert out['transcription'] is None


@pytest.mark.parametrize('value',[None,'',[],{'text':'bad'}])
def test_required_full_text_cannot_be_replaced_with_field_snippets(cloud,value):
    page,config=cloud;p={**config['models']['primary'],'full_transcription':True}
    with pytest.raises(providers.ProviderResponseError,match='逐页全文'):
        providers.CloudProvider(p,httpx.MockTransport(lambda _:reply({'fields':[],'transcription':value}))).identify(page)


def test_versions_export_and_historical_empty(client):
    doc,_=new_doc(client);identifier=doc['id'];page=doc['pages'][0]
    url='/api/documents/'+identifier
    assert client.get(url).json()['transcription_pages'][0]['versions']==[]
    result={'pages':[{'id':page['id'],'stages':[
        {'stage':'primary','status':'completed','transcription':{'text':TEXT,'model':'27b','coverage':'full_page','verified':False}},
        {'stage':'secondary','status':'completed','raw_response':{'text':'OCR original 120.00'},'requested_model':'ocr','region':[0,0,1,1]},
        {'stage':'context','status':'completed','transcription':{'text':'must not replace original','model':'27b'}},
        {'stage':'primary','status':'unknown','transcription':{'text':'partial','model':'27b'}}]}]}
    with store.connection() as db:
        db.execute('UPDATE documents SET result=? WHERE id=?',(store.dumps(result),identifier))
    versions=client.get(url).json()['transcription_pages'][0]['versions']
    assert [v['model'] for v in versions]==['27b','ocr']
    exported=client.get(url+'/transcription.txt')
    assert exported.status_code==200 and TEXT in exported.text
    assert 'OCR original 120.00' in exported.text and 'must not replace' not in exported.text
    assert exported.headers['content-type'].startswith('text/plain')
    assert client.get('/api/documents/nonexistent/transcription.txt').status_code==404


def test_cache_signature_changes_for_full_text():
    assert profile_signature({'models':{'primary':{'full_transcription':True}}})!=profile_signature({'models':{'primary':{}}})


def test_native_lines_preserve_original():
    doc={'pages':[{'id':'native','native_lines':[{'raw_text':'原文甲','text':'normalized'},{'text':'原文乙'}]}]}
    assert transcription.pages(doc)[0]['versions'][0]['text']=='原文甲\n原文乙'
