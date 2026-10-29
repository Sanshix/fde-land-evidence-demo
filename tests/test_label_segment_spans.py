import pytest

from app.deterministic_parser import labeled_segments, parse_text
from scripts.score_recognition import unique_candidate


def parse(raw):
    return parse_text(raw, {'id': 'p'}, {'model_id': 'synthetic', 'provider': 'offline'})


def test_repeated_labels_have_distinct_ocr_segments_but_one_response():
    raw = '签订日期：2009年4月24日 签订日期：2009年4月24日'
    rows = parse(raw)[0]['signed']
    assert [r['parse_provenance']['source_span'] for r in rows] == [[0, 16], [16, 31]]
    assert len({r['parse_provenance']['response_id'] for r in rows}) == 1
    for row in rows:
        p = row['parse_provenance']
        assert raw[slice(*p['source_span'])] == p['source_span_text']
        assert not p['original_location_verified'] and not row['verified'] and not row['can_publish']
    assert unique_candidate('signed', rows)[1] == 'unique_observed'
    assert parse(raw) == parse(raw)


def test_two_different_dates_remain_conflicting_and_different_roles_stay_separate():
    raw = '签订日期：2030年1月1日 签订日期：2031年1月1日'
    assert unique_candidate('signed', parse(raw)[0]['signed'])[1] == 'withheld_conflict'
    raw = '原合同编号：旧（2020）001号 合同编号：新（2030）002号'
    candidates, _ = parse(raw)
    assert len(candidates['original_contract_no']) == len(candidates['contract_no']) == 1
    old = candidates['original_contract_no'][0]['parse_provenance']
    new = candidates['contract_no'][0]['parse_provenance']
    assert old['source_span'][1] <= new['source_span'][0]


def test_nfkc_expansion_and_whitespace_offsets_point_into_original_not_normalized_text():
    raw = '　土地面积（m²）：2㎡ 联系电话：10012345678　'
    rows = parse(raw)[0]
    area = rows['area'][0]['parse_provenance']
    phone = rows['phone'][0]['parse_provenance']
    assert raw[slice(*area['source_span'])] == '土地面积（m²）：2㎡ '
    assert raw[slice(*phone['source_span'])] == '联系电话：10012345678'
    assert labeled_segments(raw) == [(k, v) for k, v, _ in labeled_segments(raw, with_spans=True)]


@pytest.mark.parametrize('raw', ['**签订日期：2030年1月1日**', '# 签订日期：2030年1月1日',
                                '<|LOC_2|>签订日期：2030年1月1日',
                                '承租人：Cafe\u0301'])
def test_wrappers_or_composed_unicode_keep_coarse_span_not_false_precision(raw):
    rows = [r for rs in parse(raw)[0].values() for r in rs]
    assert rows
    assert all(r['parse_provenance']['span_scope'] == 'line' for r in rows)
    assert all('source_span_text' not in r['parse_provenance'] for r in rows)


def test_following_line_value_retains_joined_locator_without_fabricated_crossline_offsets():
    row = parse('签订日期：\n2030-01-01')[0]['signed'][0]
    assert row['response_locator'] == 'response/line1 + response/line2'
    assert row['parse_provenance']['source_span'] is None
    assert row['bbox'] is None
