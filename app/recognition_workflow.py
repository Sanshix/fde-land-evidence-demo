"""Explicit two-model presets; callers retain authorization and enablement controls."""
import copy

WORKFLOW = 'qwen27b_with_ocr_review'
PLAN2 = 'ocr_then_qwen_text'
PLAN3 = 'ocr_then_qwen_image_text'
HIGH_END = {'plan3-qwen3.7-plus': 'qwen3.7-plus', 'plan3-qwen3-vl-plus': 'qwen3-vl-plus'}


def configure(base, primary_id='qwen3.5-27b'):
    vision_id = HIGH_END.get(primary_id, 'qwen3.5-27b')
    image_review = primary_id == 'plan3' or primary_id in HIGH_END
    if image_review:
        primary_id = 'qwen-vl-ocr-2025-11-20'
    if primary_id not in {'qwen3.5-27b', 'qwen-vl-ocr-2025-11-20'}:
        raise ValueError('请选择27B或OCR主模型')
    old = next((p for p in base.get('models', {}).values()
                if p.get('provider') == 'aliyun' and p.get('authorized')), None)
    if not old:
        raise ValueError('请先配置已获授权的阿里云端点')
    endpoint = {k: old[k] for k in ('provider', 'base_url', 'api_key_env') if k in old}
    common = dict(authorized=True, identity_verified=False, weight_revision=None)
    vision = dict(endpoint, **common, model_id='qwen3.5-27b', open_weights=True,
                  catalog_id='qwen3.5-27b', full_transcription=True, stream=True, enable_thinking=True,
                  thinking_budget=1024, max_tokens=8000,
                  license='Apache-2.0', model_card_url='https://huggingface.co/Qwen/Qwen3.5-27B')
    if vision_id != 'qwen3.5-27b':
        vision.update(model_id=vision_id, catalog_id=vision_id, open_weights=False,
                      license='commercial_api', model_card_url='https://help.aliyun.com/zh/model-studio/vision')
    ocr = dict(endpoint, **common, model_id='qwen-vl-ocr-2025-11-20',
               response_format='ocr_text', open_weights=False)
    if primary_id == ocr['model_id']:
        return {**copy.deepcopy(base), 'workflow': PLAN3 if image_review else PLAN2, 'document_context': False,
                'models': {'primary': ocr, 'secondary': {**vision, 'max_tokens': 12000, 'save_text_revision': True, 'include_original_images': image_review}},
                'reason': (f'方案3：OCR全文 → {vision_id}结合原图与全文复核、推理及结构化输出' if image_review else '方案2：逐页OCR全文 → 27B纯文本推理与结构化输出，疑点人工核验')}
    primary, secondary = (vision, ocr) if primary_id == vision['model_id'] else (ocr, vision)
    return {**copy.deepcopy(base), 'workflow': WORKFLOW, 'document_context': False,
            'models': {'primary': primary, 'secondary': secondary, 'context': {**vision, 'thinking_budget': 2048}},
            'reason': '27B视觉识别与推理；存疑时补充备用识别并由27B复核，仍须人工确认'}


def escalation_codes(config):
    if config.get('workflow') == WORKFLOW:
        # A single successful read still needs human review, but alone does not trigger paid retries.
        return {'missing', 'no_evidence', 'disagreement', 'low_score', 'invalid_format', 'cross_field_conflict'}
    return {'missing', 'no_evidence', 'single_path'}


def reported_uncertainty(stages):
    for stage in stages:
        transcript = (stage.get('transcription') or {}).get('text', '')
        if stage.get('warnings') or any(mark in transcript for mark in ('[无法确定]', '[无法辨认]')):
            return True
        raw = stage.get('raw_response') or {}
        if any(isinstance(f, dict) and f.get('status') == 'cannot_determine'
               for f in raw.get('fields', [])):
            return True
    return False
