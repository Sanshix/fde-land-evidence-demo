import json
import httpx
from test_cloud import cloud as cloud_fixture
from app.providers import CloudProvider
from app.text_comparison import compare
from app.transcription import pages as transcript_pages
cloud=cloud_fixture


def test_dependent_agreement_does_not_invent_confidence():
    page={'id':'p'};text='土地面积10平方米'
    response={'status':'completed','transcriptions':[{'source_page_id':'p','text':text}],
              'candidates':{'area':[{'source_page_id':'p','value':'10','raw_text':text}]}}
    result=compare([page],[text],response)
    assert result['pages'][0]['status']=='identical'
    field=result['fields']['area'][0]
    assert field['status']=='shared_quote' and field['independent_paths']==1
    assert field['confidence_probability'] is None and field['requires_human']
    response['transcriptions'][0]['text']='土地面积20平方米'
    result=compare([page],[text],response)
    assert result['pages'][0]['changes'][0]['original']==text
    assert result['fields']['area'][0]['status']=='changed_quote'
    response['candidates']['area'][0]['raw_text']='推断所得'
    assert compare([page],[text],response)['fields']['area'][0]['status']=='unsupported_quote'


def test_revision_mapping_rejects_wrong_duplicate_and_missing_pages(cloud):
    page,c=cloud;second={**page,'id':'second'};profile={**c['models']['primary'],'save_text_revision':True}
    seen=[]
    def handle(request):
        body=json.loads(request.content);seen.append(body)
        assert all(p['type']=='text' for p in body['messages'][0]['content'])
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps({'fields':[],
          'transcriptions':[{'page':1,'text':'OCR原文'},{'page':1,'text':'重复'},{'page':99,'text':'跨页'}]})},'finish_reason':'stop'}]})
    out=CloudProvider(profile,httpx.MockTransport(handle)).identify_document([page,second],ocr_texts=['OCR原文','第二页'])
    assert len(out['transcriptions'])==1
    assert out['transcriptions'][0]['source_page_id']==page['id']
    assert out['transcriptions'][0]['independent_evidence'] is False
    assert any(r['reason']=='missing_text_revision' for r in out['rejected'])
    doc={'pages':[page,second],'result':{'pages':[{'id':page['id'],'stages':[
      {'stage':'text_revision','status':'completed','transcription':out['transcriptions'][0]}]}]}}
    assert transcript_pages(doc)[0]['versions'][0]['text']=='OCR原文'


def test_failed_model_cannot_supply_comparison_evidence():
    result=compare([{'id':'p'}],['first'],{'status':'failed','transcriptions':[{'source_page_id':'p','text':'first'}]})
    assert result['pages'][0]['status']=='missing_revision'
