"""Fresh paired plan3 runs, changing only first-stage OCR model. No gold access."""
import concurrent.futures
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/vl-firststage-20261029'
MODELS = ['qwen-vl-ocr-2025-11-20', 'qwen3-vl-plus']
OCR_PROMPT = 'Please output only the text content from the image without any additional descriptions or formatting.'


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    source = RUN / 'source'
    if not source.exists():
        for directory in ['app', 'config']:
            shutil.copytree(ROOT / directory, source / directory,
                            ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        shutil.copy2(Path(__file__), RUN / 'benchmark-script.py')
    sys.path.insert(0, str(source))
    # The launcher may have imported project credentials to supply env values in memory.
    # Explicitly bind subsequent app imports to the saved code, not the live checkout.
    if 'app' in sys.modules:
        sys.modules['app'].__path__ = [str(source / 'app')]
    from app.providers import CloudProvider, ProviderResponseError, base_configuration, approved_input, image_payload
    from app.recognition_workflow import configure
    from app.domain import FIELDS

    config = configure(base_configuration(), 'plan3')
    review = config['models']['secondary']
    assert review['model_id'] == 'qwen3.5-27b' and review['max_tokens'] == 12000
    specialist = {**config['models']['primary'], 'ocr_prompt': OCR_PROMPT}
    vl = {**specialist, 'model_id': 'qwen3-vl-plus', 'stream': True,
          'enable_thinking': False, 'max_tokens': 3000, 'license': 'commercial_api'}
    profiles = dict(zip(MODELS, [specialist, vl]))
    contracts = json.loads((ROOT/'runs/highend-models-20261029/manifest.json').read_text())['contracts']
    # Freeze an exact input allowlist; copied code has a different sample-root path.
    config = {**config, 'authorized_scope': 'explicit_sample_hash',
              'authorized_sha256': [p['sha256'] for c in contracts for p in c['files']]}
    manifest = {'contracts': contracts, 'ocr_models': MODELS, 'review_profile': review,
                'ocr_profiles': profiles, 'ocr_prompt': OCR_PROMPT, 'ocr_max_tokens': 3000,
                'vl_thinking': False, 'review_thinking_budget': 1024, 'review_max_tokens': 12000,
                'concurrency': 3, 'repetitions': 1, 'retry': False,
                'authorized_sha256': config['authorized_sha256'],
                'ordering': 'alternate model order by contract; both branches fresh OCR and fresh 27B',
                'scope': 'same ten development contracts; no blind test or character-level gold',
                'script_sha256': sha(Path(__file__)),
                'source_sha256': {str(p.relative_to(source)): sha(p) for p in sorted(source.rglob('*.py'))}}
    if (RUN/'manifest.json').exists():
        assert json.loads((RUN/'manifest.json').read_text()) == manifest
    save(RUN/'manifest.json', manifest)
    docs = [{'name': c['name'], 'files': [p['name'] for p in c['files']]} for c in contracts]
    jobs = []
    for index, contract in enumerate(contracts):
        pages = []
        for i, record in enumerate(contract['files'], 1):
            path = ROOT.parent/'练习集/01_合同扫描件'/record['name']
            assert sha(path) == record['sha256']
            pages.append({'id': f"{contract['name']}-{i}", 'path': str(path), 'source': str(path), 'name': path.name})
            for profile in profiles.values():
                approved_input(pages[-1], config, profile)
        order = MODELS if index % 2 == 0 else MODELS[::-1]
        jobs.extend((m, contract['name'], pages) for m in order)
    for model in MODELS:
        save(RUN/model/'documents.json', docs)

    class RecordingProvider(CloudProvider):
        def complete(self, body, timeout):
            p = self.profile
            self.request = dict(body)
            if 'enable_thinking' in p:
                self.request.update(enable_thinking=p['enable_thinking'],
                                    thinking_budget=p.get('thinking_budget', 4096), max_tokens=p['max_tokens'])
            if p.get('stream'):
                self.request.update(stream=True, stream_options={'include_usage': True})
            self.response = super().complete(body, timeout)
            return self.response

    from PIL import Image, ImageDraw
    canvas = Image.new('RGB', (600, 120), 'white')
    ImageDraw.Draw(canvas).text((30, 35), 'TEST 12345', fill='black', font_size=40)
    canvas.save(RUN/'synthetic.png')
    encoded, _ = image_payload(RUN/'synthetic.png')
    probe = RecordingProvider(vl)
    response = probe.complete({'model': vl['model_id'], 'max_tokens': 3000, 'temperature': 0,
        'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': OCR_PROMPT},
                      {'type': 'image_url', 'image_url': {'url': encoded}}]}]}, timeout=90)
    save(RUN/'probe.json', response)
    assert response['choices'][0]['finish_reason'] == 'stop'
    assert 'TEST 12345' in response['choices'][0]['message']['content']
    print('VL-Plus OCR-only non-thinking probe passed', flush=True)

    def one(job):
        model, name, pages = job
        folder = RUN/model/'recognition'/name
        if (folder/'outcome.json').exists():
            return json.loads((folder/'outcome.json').read_text())
        started = time.monotonic()
        texts, stages, ocr_candidates = [], [], {k: [] for k in FIELDS}
        error = None
        final = {'candidates': {k: [] for k in FIELDS}, 'completion_status': 'failed',
                 'page_coverage': {'completed': [], 'total': len(pages)}}
        def invoke(provider, label, call):
            stamp = time.monotonic()
            try:
                result = call()
                save(folder/f'{label}-response.json', result)
                return result
            except ProviderResponseError as exc:
                save(folder/f'{label}-error-evidence.json', exc.evidence)
                raise
            finally:
                if hasattr(provider, 'request'):
                    save(folder/f'{label}-request.json', provider.request)
                if hasattr(provider, 'response'):
                    save(folder/f'{label}-transport-response.json', provider.response)
                usage = getattr(provider, 'response', {}).get('usage')
                stages.append({'stage': 'review' if label == 'review' else 'ocr', 'label': label,
                               'seconds': time.monotonic()-stamp, 'usage': usage})
        try:
            for page in pages:
                provider = RecordingProvider(profiles[model])
                result = invoke(provider, page['id']+'-ocr', lambda: provider.identify(page, timeout=150))
                text = (result.get('transcription') or {}).get('text')
                if not text:
                    raise ValueError('Empty full-page OCR')
                texts.append(text)
                for key, values in result['candidates'].items():
                    ocr_candidates[key].extend(values)
            # A separate OCR-only extraction diagnostic uses the same deterministic parser.
            save(RUN/(model+'-ocr-only')/'recognition'/name/'result.json',
                 {'candidates': ocr_candidates, 'completion_status': 'completed'})
            provider = RecordingProvider(review)
            result = invoke(provider, 'review', lambda: provider.identify_document(pages, timeout=240, ocr_texts=texts))
            final = {'candidates': result['candidates'], 'completion_status': 'partial' if result.get('rejected') else 'completed',
                     'page_coverage': {'completed': [p['id'] for p in pages], 'total': len(pages)},
                     'rejected': result.get('rejected', [])}
        except Exception as exc:
            error = {'type': type(exc).__name__, 'message': str(exc)}
        outcome = {'name': name, 'ocr_model': model, 'review_model': review['model_id'], 'pages': len(pages),
                   'seconds': time.monotonic()-started, 'stages': stages, 'error': error,
                   'completion_status': final['completion_status'], 'published': False}
        save(folder/'result.json', final)
        save(folder/'outcome.json', outcome)
        save(RUN/(model+'-ocr-only')/'recognition'/name/'outcome.json',
             {**outcome, 'completion_status': 'completed' if len(texts) == len(pages) else 'failed'})
        print(json.dumps({k: v for k, v in outcome.items() if k != 'stages'}, ensure_ascii=False), flush=True)
        return outcome

    for model in MODELS:
        save(RUN/(model+'-ocr-only')/'documents.json', docs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(one, jobs))
    save(RUN/'inference-complete.json', {'at': time.time(), 'jobs': len(outcomes)})


if __name__ == '__main__':
    main()
