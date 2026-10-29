from app.deterministic_parser import parse_text
from scripts.score_recognition import unique_candidate


def parse(text):
    return parse_text(text, {'id': 'same-page'}, {'model_id': 'fixture', 'provider': 'offline'})


def test_repeated_label_with_distinct_following_dates_cannot_erase_conflict():
    candidates, audit = parse('签订日期：\n2030-01-01\n签订日期：\n2031-01-01')
    assert unique_candidate('signed', candidates['signed'])[1] == 'withheld_conflict'
    assert {c['response_locator'] for c in candidates['signed']} == {
        'response/line1 + response/line2', 'response/line3 + response/line4'}
    assert audit['duplicate_lines_observed'] == 1


def test_same_value_two_positions_is_one_value_not_independent_verification():
    raw = '土地面积2㎡\n土地面积2㎡'
    candidates, audit = parse(raw)
    assert len(candidates['area']) == 2
    assert unique_candidate('area', candidates['area'])[1] == 'unique_observed'
    assert len({c['parse_provenance']['response_id'] for c in candidates['area']}) == 1
    assert all(not c['verified'] and not c['can_publish'] for c in candidates['area'])
    assert parse(raw) == (candidates, audit)


def test_different_values_remain_conflicting_and_raw_response_lines_remain():
    raw = '土地面积2㎡\n土地面积3㎡\n土地面积2㎡'
    candidates, audit = parse(raw)
    assert unique_candidate('area', candidates['area'])[1] == 'withheld_conflict'
    assert [c['raw_text'] for c in candidates['area']] == raw.splitlines()
    assert len({c['response_locator'] for c in candidates['area']}) == 3
    assert audit['duplicate_lines_excluded_from_extraction'] == 0
