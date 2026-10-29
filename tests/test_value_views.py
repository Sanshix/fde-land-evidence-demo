import copy

import pytest

from app.value_views import comparison_view, conflict_diagnosis


@pytest.mark.parametrize('value', ['文租（２０２６）第００１号', '文租 (2026) 第001号', '文租〔2026〕第001号'])
def test_identifier_syntax_views_preserve_facts_and_leading_zeros(value):
    view = comparison_view('contract_no', value)
    assert view['original_value'] == view['display_value'] == value
    assert view['score_comparison_value'] == '文租〔2026〕第001号'
    assert comparison_view('contract_no', view['score_comparison_value'])['score_comparison_value'] == view['score_comparison_value']
    assert not view['publication_allowed'] and not view['changes_source_fact']


@pytest.mark.parametrize('field,value', [('tenant', '甲（乙）公司'), ('area', '2㎡'),
                                          ('contract_no', '文租（2026]001号'),
                                          ('contract_no', '文租（2025）（2026）001号'),
                                          ('address', 'xx镇1号')])
def test_nonprotocol_values_are_literal(field, value):
    assert comparison_view(field, value)['score_comparison_value'] == value


def test_prefix_serial_role_and_placeholder_are_not_scoring_equivalences():
    def key(s):
        return comparison_view('contract_no', s)['score_comparison_value']
    assert key('文恒(2026)001号') != key('文租(2026)001号')
    assert key('文租(2026)001号') != key('文租(2026)01号')
    assert key('xx文租(2026)001号') != key('××文租(2026)001号')
    assert comparison_view('original_contract_no', '文租(2026)001号')['field'] == 'original_contract_no'


def test_diagnosis_never_removes_true_conflicts_or_votes_same_response():
    rows = [{'value': '1', 'raw_text': '旧1新2', 'source_page_id': 'p1', 'text_role': 'crossed_out',
             'parse_provenance': {'response_id': 'r', 'response_locator': 'line1'}},
            {'value': '2', 'raw_text': '旧1新2', 'source_page_id': 'p1', 'text_role': 'current',
             'parse_provenance': {'response_id': 'r', 'response_locator': 'line1'}}]
    before = copy.deepcopy(rows)
    result = conflict_diagnosis('area', rows)
    assert result['category'] == 'source_alteration_or_explicit_alternative'
    assert result['response_count'] == 1 and result['duplicates_not_independent_evidence'] == 1
    assert rows == before and not result['automatic_value_selection']


def test_anonymous_prefix_triage_does_not_equate_facts_or_english_company_letters():
    rows = [{'value': x} for x in ['**镇1号', '×镇1号', 'xx镇1号']]
    assert conflict_diagnosis('address', rows)['category'] == 'possible_anonymous_prefix_disagreement_not_equivalent'
    assert len({comparison_view('address', x['value'])['score_comparison_value'] for x in rows}) == 3
    assert conflict_diagnosis('tenant', [{'value': 'XCorp'}, {'value': '×Corp'}])['category'] != 'possible_anonymous_prefix_disagreement_not_equivalent'
