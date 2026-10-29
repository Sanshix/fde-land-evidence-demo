"""Paired model-only comparison; answers are read by the separate offline reporter."""
import concurrent.futures
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / 'runs/highend-models-20261029'
SOURCE = RUN / 'source'
MODELS = ['qwen3.5-27b', 'qwen3.7-plus', 'qwen3-vl-plus']


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze():
    if SOURCE.exists():
        return
    for directory in ['app', 'config']:
        shutil.copytree(ROOT / directory, SOURCE / directory,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    save(RUN / 'source-sha256.json', {str(p.relative_to(SOURCE)): sha(p)
         for p in sorted(SOURCE.rglob('*')) if p.is_file()})


def main():
    freeze()
    sys.path.insert(0, str(SOURCE))
    from app.providers import CloudProvider, ProviderResponseError, base_configuration
    from app.recognition_workflow import configure
    from app.domain import FIELDS

    config = configure(base_configuration(), 'plan3')
    base = {**config['models']['secondary'], 'max_tokens': 12000, 'thinking_budget': 1024}
    prior = ROOT / 'runs/paired-qwen-ocr-10-20261029'
    contracts = json.loads((prior / 'manifest.json').read_text())['contracts']
    manifest = {'contracts': contracts, 'models': MODELS, 'repetitions': 1, 'concurrency': 3,
                'thinking_budget': 1024, 'max_tokens': 12000, 'temperature': 0,
                'protocol': 'same frozen OCR + all original images; current production plan3 prompt; no fallback/retry',
                'selection': 'same first ten development contracts as prior run; not blind or holdout',
                'ordering': 'interleave all models per contract, rotate model order by contract index',
                'ocr_source': str(prior.relative_to(ROOT) / 'plan2'),
                'script_sha256': sha(Path(__file__)), 'source_manifest_sha256': sha(RUN / 'source-sha256.json'),
                'profile': {k: v for k, v in base.items() if k not in {'api_key', 'token'}}}
    if (RUN / 'manifest.json').exists():
        assert json.loads((RUN / 'manifest.json').read_text()) == manifest
    save(RUN / 'manifest.json', manifest)
    jobs = []
    for index, contract in enumerate(contracts):
        name = contract['name']
        pages = []
        texts = []
        for i, record in enumerate(contract['files'], 1):
            path = ROOT.parent / '练习集/01_合同扫描件' / record['name']
            assert sha(path) == record['sha256']
            pages.append({'id': f'{name}-{i}', 'name': path.name, 'path': str(path), 'source': str(path)})
            ocr_path = prior / 'plan2/recognition' / name / f'{name}-{i}-ocr.json'
            response = json.loads(ocr_path.read_text())
            text = (response.get('transcription') or {}).get('text')
            assert text and isinstance(text, str)
            texts.append(text)
            save(RUN / 'ocr' / name / ocr_path.name, response)
        order = MODELS[index % 3:] + MODELS[:index % 3]
        jobs.extend((model, name, pages, texts) for model in order)
    docs = [{'name': c['name'], 'files': [p['name'] for p in c['files']]} for c in contracts]
    for model in MODELS:
        save(RUN / model / 'documents.json', docs)

    # Synthetic vision probe before sending any contracts. No automatic substitution.
    from PIL import Image, ImageDraw
    canvas = Image.new('RGB', (600, 120), 'white')
    ImageDraw.Draw(canvas).text((30, 35), 'TEST 12345', fill='black', font_size=40)
    canvas.save(RUN / 'synthetic.png')
    from app.providers import image_payload
    encoded, _ = image_payload(RUN / 'synthetic.png')
    probes = []
    for model in MODELS:
        profile = {**base, 'model_id': model}
        start = time.monotonic()
        response = CloudProvider(profile).complete({'model': model, 'temperature': 0,
            'messages': [{'role': 'user', 'content': [
                {'type': 'text', 'text': 'Read the image. Return only the visible text.'},
                {'type': 'image_url', 'image_url': {'url': encoded}}]}]}, timeout=90)
        probe = {'model': model, 'seconds': time.monotonic() - start, 'response': response}
        probes.append(probe)
        save(RUN / 'probes.json', probes)
        assert response['choices'][0]['finish_reason'] == 'stop'
        assert 'TEST 12345' in response['choices'][0]['message']['content']
        print(json.dumps({'probe': model, 'seconds': probe['seconds'], 'success': True}), flush=True)

    class RecordingProvider(CloudProvider):
        def complete(self, body, timeout):
            # The provider applies these exact parameters at transport time too.
            self.request = {**body, 'stream': True, 'stream_options': {'include_usage': True},
                            'enable_thinking': True, 'thinking_budget': 1024, 'max_tokens': 12000}
            self.response = super().complete(body, timeout)
            return self.response

    def one(job):
        model, name, pages, texts = job
        folder = RUN / model / 'recognition' / name
        if (folder / 'outcome.json').exists():
            return json.loads((folder / 'outcome.json').read_text())
        profile = {**base, 'model_id': model, 'catalog_id': model}
        if model != MODELS[0]:
            profile.update(open_weights=False, license='commercial_api')
        provider = RecordingProvider(profile)
        start = time.monotonic()
        error = None
        result = {'candidates': {k: [] for k in FIELDS}, 'completion_status': 'failed',
                  'page_coverage': {'completed': [], 'total': len(pages)}}
        response = None
        try:
            response = provider.identify_document(pages, timeout=240, ocr_texts=texts)
            save(folder / 'qwen-response.json', response)
            result = {'candidates': response['candidates'],
                      'completion_status': 'partial' if response.get('rejected') else 'completed',
                      'page_coverage': {'completed': [p['id'] for p in pages], 'total': len(pages)},
                      'rejected': response.get('rejected', [])}
        except Exception as exc:
            error = {'type': type(exc).__name__, 'message': str(exc)}
            if isinstance(exc, ProviderResponseError):
                save(folder / 'response-error-evidence.json', exc.evidence)
        finally:
            if hasattr(provider, 'request'):
                save(folder / 'qwen-request.json', provider.request)
            if hasattr(provider, 'response'):
                save(folder / 'transport-response.json', provider.response)
        seconds = round(time.monotonic() - start, 3)
        usage = (response or getattr(provider, 'response', {})).get('usage')
        outcome = {'name': name, 'model': model, 'pages': len(pages), 'seconds': seconds,
                   'timing_scope': 'review only; same cached OCR excluded for all models',
                   'stages': [{'stage': 'review', 'seconds': seconds, 'usage': usage}],
                   'error': error, 'published': False, 'completion_status': result['completion_status']}
        save(folder / 'result.json', result)
        save(folder / 'outcome.json', outcome)
        print(json.dumps(outcome, ensure_ascii=False), flush=True)
        return outcome

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(one, jobs))
    save(RUN / 'inference-complete.json', {'at': time.time(), 'jobs': len(outcomes)})


if __name__ == '__main__':
    main()
