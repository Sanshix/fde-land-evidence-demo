"""Dependent text comparison: change evidence, never calibrated recognition confidence."""
import difflib
import re


def compact(text):
    return re.sub(r'\s+', '', text or '')


def compare(pages, originals, response, image_grounded=False):
    revised = {t['source_page_id']: t['text'] for t in response.get('transcriptions', [])
               if response.get('status') == 'completed'}
    sources = dict(zip((p['id'] for p in pages), originals))
    rows=[]
    for page in pages:
        original=sources[page['id']];revision=revised.get(page['id'])
        changes=[]
        if revision is not None:
            # Bound alignment work; each character remains present in its saved full version.
            before,after=original.splitlines(keepends=True),revision.splitlines(keepends=True)
            for tag,a,b,c,d in difflib.SequenceMatcher(None,before,after,autojunk=True).get_opcodes():
                if tag!='equal':
                    changes.append({'type':tag,'original_lines':[a+1,b], 'revised_lines':[c+1,d],
                                    'original':''.join(before[a:b]),'revised':''.join(after[c:d])})
        rows.append({'source_page_id':page['id'],'status':'missing_revision' if revision is None else 'identical' if revision==original else 'changed',
                     'changes':changes,'independent_evidence':False,'confidence_probability':None})
    fields={}
    for key,candidates in response.get('candidates',{}).items():
        checked=[]
        for candidate in candidates:
            pid=candidate.get('source_page_id');quote=compact(candidate.get('raw_text',''))
            in_first=bool(quote) and quote in compact(sources.get(pid,''))
            in_second=bool(quote) and quote in compact(revised.get(pid,''))
            status=('missing_revision' if pid not in revised else 'unsupported_quote' if not in_first
                    else 'shared_quote' if in_second else 'changed_quote')
            checked.append({'value':candidate['value'],'source_page_id':pid,'raw_text':candidate.get('raw_text',''),
                            'status':status,'quote_in_ocr':in_first,'quote_in_revision':in_second,
                            'confidence_probability':None,'independent_paths':1,'requires_human':True})
        if checked:fields[key]=checked
    return {'basis':('复核模型结合原图与首遍OCR复核；文字一致不直接代表识别正确，也不构成独立盲读证据。' if image_grounded else '复核模型只读取OCR文字，两遍结果存在依赖；一致不等于识别正确，不增加独立路径或正确概率。'),
            'pages':rows,'fields':fields}
