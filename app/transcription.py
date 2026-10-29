"""Expose page text without treating field snippets or reasoning as a full transcript."""


def pages(doc):
    results = {row['id']: row for row in (doc.get('result') or {}).get('pages', [])}
    output = []
    for page in doc.get('pages', []):
        versions = []
        if page.get('native_lines'):
            text = '\n'.join(str(line.get('raw_text', line.get('text', ''))) for line in page['native_lines'])
            if text.strip():
                versions.append({'text': text, 'model': 'native_extract', 'stage': 'native',
                                 'coverage': 'native_unit', 'verified': False})
        for stage in results.get(page['id'], {}).get('stages', []):
            if stage.get('status') != 'completed' or stage.get('stage') == 'context':
                continue
            transcript = stage.get('transcription')
            # Older OCR responses explicitly carry text; field raw_text never substitutes for it.
            if not transcript and isinstance(stage.get('raw_response'), dict):
                text = stage['raw_response'].get('text')
                if isinstance(text, str) and text.strip():
                    transcript = {'text': text, 'model': stage.get('requested_model'),
                                  'coverage': 'legacy_scope_unknown',
                                  'verified': False}
            if isinstance(transcript, dict) and isinstance(transcript.get('text'), str) and transcript['text'].strip():
                versions.append({**transcript, 'stage': stage['stage']})
        output.append({'id': page['id'], 'name': page.get('name', page['id']), 'versions': versions})
    return output


def export(doc):
    output = ['模型文字转写，尚未人工核验；各版本分别保留，不能代替原件。模型文字复核稿的输入来源见版本说明，两遍一致不直接代表识别正确。']
    for page in pages(doc):
        output.append(f"\n=== {page['name']} ===")
        if not page['versions']:
            output.append('本页暂无完整文字转写；字段片段不等于全文。')
        for version in page['versions']:
            output.append(f"\n[{version['stage']} / {version['model']} / {version['coverage']} / {version.get('input_source','original_source')}]\n{version['text']}")
    return '\n'.join(output) + '\n'
