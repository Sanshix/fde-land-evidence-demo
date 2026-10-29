from copy import deepcopy

from app.evidence import normalized_candidates
from app.identifier_templates import comparison_key, describe


def test_template_preserves_source_and_serial():
    raw = '××自然资租变（2025）007号'
    result = describe(raw)
    assert result['ocr_value'] == raw
    assert result['normalized_value'] == '××自然资租变〔2025〕007号'
    assert not result['human_verified']
    assert 'sample' in result['basis']['authority']
    assert describe('××国土租字〔2002〕第019号')['normalized_value'] == '××国土租字（2002）第019号'


def test_outside_scope_and_mismatched_brackets():
    for raw in ['真实部门（2025）007号', '××自然资租变（2099）007号', '××自然资租变（2025〕007号']:
        assert describe(raw)['normalized_value'] is None


def test_comparison_does_not_repair_digits_or_prefix():
    base = '甲（2025）007号'
    assert comparison_key(base) == comparison_key('甲〔2025〕007号')
    for other in ['甲〔2025〕017号', '甲〔2024〕007号', '乙〔2025〕007号', '甲〔2025〕7号']:
        assert comparison_key(base) != comparison_key(other)


def test_candidates_not_mutated_and_idempotent():
    source = {'contract_no': [{'value': '××自然资租变（2025）007号', 'raw_text': '原文'},
                              {'value': '××自然资租变〔2025〕007号', 'raw_text': '另一处'}]}
    saved = deepcopy(source)
    result = normalized_candidates(source)
    assert source == saved
    assert len({c['value'] for c in result['contract_no']}) == 1
    assert normalized_candidates(result) == result
    assert result['contract_no'][0]['identifier_format']['ocr_value'] == source['contract_no'][0]['value']


def test_mixed_template_is_disabled():
    assert describe('甲（2025）007号', {'rules': []})['normalized_value'] is None
