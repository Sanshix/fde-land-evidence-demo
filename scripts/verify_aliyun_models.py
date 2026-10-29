"""Probe model IDs using synthetic input only; never prints credentials."""
import base64
import io
import json
import sys
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.credentials import credential

ROOT = Path(__file__).resolve().parents[1]
HOST = 'ws-gii3yuqz050k1sww.cn-beijing.maas.aliyuncs.com'
BASE = f'https://{HOST}/compatible-mode/v1'

def main():
    target = ROOT / 'runs/model-id-verification-20261029'
    target.mkdir(parents=True, exist_ok=True)
    canvas = Image.new('RGB', (600, 120), 'white')
    ImageDraw.Draw(canvas).text((30, 35), 'TEST 12345', fill='black', font_size=40)
    buffer = io.BytesIO()
    canvas.save(buffer, format='PNG')
    url = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()
    results = []
    with httpx.Client(headers={'Authorization': 'Bearer '+credential('AI_ALIYUN_KEY', HOST)}, timeout=45, trust_env=False) as client:
        listing = client.get(BASE+'/models')
        listing.raise_for_status()
        catalog = listing.json()
        (target/'models.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2))
        ids = {m['id'] for m in catalog.get('data', [])}
        for model, visual in [('qwen3-vl-8b-thinking', True), ('qwen3-32b', False), ('qwen3.5-27b', True), ('qwen3.5-27b', False)]:
            content = [{'type':'text','text':'Read the image. Return only the visible text.'}, {'type':'image_url','image_url':{'url':url}}] if visual else 'Return only the result of 17 * 19.'
            body = dict(model=model, messages=[{'role':'user','content':content}], stream=True, max_tokens=2048)
            if model != 'qwen3-vl-8b-thinking':
                body['enable_thinking'] = True
                body['thinking_budget'] = 1024
            start = time.monotonic()
            response = client.post(BASE+'/chat/completions', json=body)
            item = dict(model=model, test='synthetic image' if visual else 'synthetic text', listed=model in ids, http_status=response.status_code, elapsed_seconds=round(time.monotonic()-start,3))
            if response.status_code == 200:
                output = ''; reasoning = ''; finish = None
                for line in response.text.splitlines():
                    if not line.startswith('data: ') or line == 'data: [DONE]': continue
                    event = json.loads(line[6:])
                    if event.get('error'): item['stream_error'] = event['error']
                    if event.get('model'): item['returned_model'] = event['model']
                    for choice in event.get('choices', []):
                        delta = choice.get('delta', {})
                        output += delta.get('content') or ''
                        reasoning += delta.get('reasoning_content') or ''
                        finish = choice.get('finish_reason') or finish
                    if event.get('usage'): item['usage'] = event['usage']
                item.update(output=output, reasoning_characters=len(reasoning), finish_reason=finish, success=finish=='stop' and ('TEST 12345' in output if visual else output.strip()=='323'))
            else:
                item['error'] = response.json().get('error')
            results.append(item)
            print(json.dumps(item,ensure_ascii=False), flush=True)
    (target/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))

if __name__ == '__main__': main()
