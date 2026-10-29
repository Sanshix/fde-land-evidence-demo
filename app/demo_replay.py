"""Replay preserved provider evidence through current product extraction and quality gates.

No network, answers, registry values or precomputed candidates enter extraction.
"""
import copy
import hashlib
import json
import time
from pathlib import Path

from .cloud_cascade import finish_status, profile_signature
from .domain import FIELDS
from .governance import parse_ocr_text
from .ocr_layout import baidu_candidates
from .providers import parse_reply
from .recognition import assess, quality


def replay_document(document, workspace, run_dir):
    started = time.monotonic()
    path = Path(workspace['cache']) / 'recognition' / document['name'] / 'result.json'
    if not path.is_file():
        raise ValueError('该文档没有冻结缓存响应；未改用实时识别')
    cache_config = json.loads((Path(workspace['cache']) / 'providers.json').read_text())
    if profile_signature(cache_config) != workspace['cache_profile']:
        raise ValueError('冻结模型条件已变化；未发送实时请求')
    allowed = {(p.get('provider'), p.get('model_id')) for p in cache_config.get('models', {}).values() if p}
    saved = json.loads(path.read_text())
    by_hash = {}
    for page in document['pages']:
        original = Path(page.get('source') or page['path'])
        digest = hashlib.sha256(original.read_bytes()).hexdigest()
        if digest in by_hash:
            raise ValueError('重复页面哈希不能唯一映射缓存')
        by_hash[digest] = page
    if len(saved.get('pages', [])) != len(by_hash):
        raise ValueError('缓存页数与导入原件不一致')
    candidates, pages = {k: [] for k in FIELDS}, []
    for old in saved['pages']:
        page = by_hash.pop(old.get('source_sha256'), None)
        if page is None:
            raise ValueError('图片哈希与缓存不匹配；未启用付费识别')
        row = {'id': page['id'], 'source_sha256': old['source_sha256'], 'quality': quality(page['path']), 'stages': []}
        for stage in old.get('stages', []):
            replay = copy.deepcopy(stage)
            replay.update(mode='frozen_replay', sent=False, new_calls=0,
                          historical_elapsed_seconds=stage.get('elapsed_seconds'), elapsed_seconds=0)
            replay.pop('candidates', None)
            if stage.get('status') == 'completed':
                if (stage.get('provider'), stage.get('requested_model')) not in allowed:
                    raise ValueError('冻结响应模型不属于声明的缓存条件')
                raw = stage.get('raw_response')
                if not isinstance(raw, dict):
                    raise ValueError('冻结响应缺少原始证据')
                profile = {'provider': stage['provider'], 'model_id': stage['requested_model'],
                           'response_id': stage.get('response_id')}
                region = stage.get('region') or [0, 0, row['quality']['width'], row['quality']['height']]
                if 'text' in raw:
                    extracted, diagnostics = parse_ocr_text(raw['text'], page, profile)
                elif 'pages' in raw:
                    extracted, diagnostics = baidu_candidates(raw, page, profile, region)
                elif 'fields' in raw:
                    extracted = parse_reply(raw, page, region, profile,
                                            'context_suggestion' if stage.get('stage') == 'context' else 'ocr')
                    diagnostics = getattr(extracted, 'diagnostics', {})
                else:
                    raise ValueError('冻结响应协议不支持；未把缓存候选直接录入')
                replay['candidates'], replay['extraction_diagnostics'] = extracted, diagnostics
                for key, values in extracted.items():
                    candidates[key].extend(values)
            row['stages'].append(replay)
        pages.append(row)
    result = {'mode': 'frozen_replay', 'strategy': 'demo_replay', 'candidates': candidates, 'pages': pages,
              'elapsed_seconds': round(time.monotonic() - started, 3), 'external_calls': 0,
              'risks': assess(candidates, [p['quality'] for p in pages]),
              'fallback_pending': False, 'cost': None, 'cost_note': '历史响应回放，本次没有模型请求；历史费用未知',
              'model_profiles': [{'provider': p, 'model_id': m} for p, m in sorted(allowed)],
              'replay': {'new_ocr': False, 'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'cache_profile': workspace['cache_profile']}}
    finish_status(result, document['pages'])
    folder = Path(run_dir)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'frozen-replay.json').write_text(json.dumps(result, ensure_ascii=False))
    return result
