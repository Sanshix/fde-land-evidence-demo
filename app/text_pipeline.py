"""Two-stage OCR followed by text-only or image-and-text document inference."""
import hashlib
import json
import time
from pathlib import Path
from .cloud_attempts import AttemptRunner, atomic_json
from .providers import CloudProvider, model_profile, approved_input, ProviderUnavailable
from .domain import FIELDS
from .recognition import assess


def run_text_pipeline(pages, run_dir, strategy, budget, config, transport,
                      defer_fallback, primary_result, retry_failed, retry_unknown):
    from .cloud_cascade import run_cloud_cascade, profile_signature
    started=time.monotonic()
    previous_elapsed=(primary_result or {}).get('elapsed_seconds',0)
    folder=Path(run_dir)
    if primary_result is None:
        first_config={**config,'workflow':None,'models':{'primary':config['models']['primary']}}
        result=run_cloud_cascade(pages,folder,'primary',budget,first_config,transport,
                                 retry_failed=retry_failed,retry_unknown=retry_unknown)
    else:
        result=json.loads(json.dumps(primary_result))
    result['strategy']=config['workflow']
    result['model_profiles']={'enabled':config.get('enabled'),'workflow':config['workflow'],
                             'models':{k:{'model_id':p['model_id'],'provider':p['provider']} for k,p in config['models'].items()}}
    image_pages=[p for p in pages if p.get('path') and (p.get('kind')!='pdf_page' or p.get('needs_ocr',True))]
    if not image_pages:
        return result
    by_id={row['id']:row for row in result.get('pages',[])}
    texts=[]
    for page in image_pages:
        stage=next((s for s in by_id.get(page['id'],{}).get('stages',[]) if s['stage']=='primary' and s['status']=='completed'),{})
        text=(stage.get('transcription') or {}).get('text')
        if not text:
            result.update(fallback_pending=False,completion_status='partial' if texts else 'failed',recoverable=True)
            atomic_json(folder/'result.json',result)
            return result
        texts.append(text)
    result['fallback_pending']=strategy!='primary'
    if defer_fallback or strategy=='primary':
        if result['fallback_pending']:result['completion_status']='partial'
        atomic_json(folder/'result.json',result)
        return result
    runner=AttemptRunner(folder,max(0,budget-(time.monotonic()-started)),transport=transport,
                         retry_failed=retry_failed,retry_unknown=retry_unknown)
    profile=model_profile(config,'secondary')
    for page in image_pages:approved_input(page,config,profile)
    key=hashlib.sha256(json.dumps({'workflow':'text-v4-source-semantics','profiles':profile_signature(config),'pages':[(p['id'],t,hashlib.sha256(Path(p['path']).read_bytes()).hexdigest()) for p,t in zip(image_pages,texts)]},sort_keys=True).encode()).hexdigest()
    response=runner.call(key,'secondary',profile,
        lambda timeout:CloudProvider(profile,transport).identify_document(image_pages,timeout=timeout,ocr_texts=texts),limit=180)
    result['text_inference']=response
    from .text_comparison import compare
    result['text_comparison']=compare(image_pages,texts,response,image_grounded=bool(profile.get('include_original_images')))
    for row in result.get('pages',[]):
        row['stages']=[s for s in row.get('stages',[]) if s.get('stage')!='text_revision']
        for text in response.get('transcriptions',[]) if response.get('status')=='completed' else []:
            if text['source_page_id']==row['id']:
                row['stages'].append({'stage':'text_revision','status':'completed','transcription':text})
    # OCR observations stay in stage evidence; final fields come from document extraction.
    result['candidates']=response.get('candidates') or {k:[] for k in FIELDS}
    result['risks']=assess(result['candidates'])
    from .observation_audit import audit
    result['observation_audit']=audit(response)
    for row in result['observation_audit']['alteration_pages']:
        result['risks'].append({'field':None,'code':'alteration_requires_confirmation','reason':'涂改必须人工核验，候选完整性：'+row['status']})
    for key, rows in result['candidates'].items():
        for candidate in rows:
            candidate['text_cross_check']=result['text_comparison']['fields'].get(key,[])
    for field, checks in result['text_comparison']['fields'].items():
        if any(c['status'] in {'unsupported_quote','changed_quote','missing_revision'} for c in checks):
            result['risks'].append({'field':field,'code':'text_quote_unsupported','reason':'两遍文字的字段引用发生变化或缺少原文支持，须核验原图'})
    for row in result['text_comparison']['pages']:
        if row['status']!='identical':
            result['risks'].append({'field':None,'code':'text_revision_review','reason':'两遍文字有改动或缺失，请核对原图：'+row['source_page_id']})
    if response.get('status')!='completed':
        result['risks'].append({'field':None,'code':'text_inference_failed','reason':'模型复核未完成，不能把OCR完成当成完整流程完成'})
    else:
        for warning in response.get('warnings',[]):
            result['risks'].append({'field':None,'code':'model_warning','reason':str(warning)})
    result.update(fallback_pending=False,state='needs_human',
                  completion_status='completed' if response.get('status')=='completed' and not response.get('rejected') else 'partial',
                  recoverable=response.get('status')!='completed' or bool(response.get('rejected')),
                  external_calls=result.get('external_calls',0)+runner.external_calls,
                  elapsed_seconds=round(previous_elapsed+time.monotonic()-started,3))
    atomic_json(folder/'result.json',result)
    return result
