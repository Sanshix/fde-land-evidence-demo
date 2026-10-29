import pytest

from app.deterministic_parser import admit_value, parse_text


@pytest.mark.parametrize('raw,value', [
    ('2㎡，四至：东至2号，南至空地，西至道路，北至3号。', '2'),
    ('19.25平方米,四至:东至空地,南至邻居,西至道路,北至宅。', '19.25'),
    ('1,234.50m²；四至：东至空地，南至道路，西至道路，北至宅。', '1234.50'),
])
def test_explicit_boundary_preserves_raw_evidence_and_is_idempotent(raw, value):
    text = '土地面积' + raw
    result, audit = parse_text(text, {'id': 'page'}, {'provider': 'test', 'model_id': 'test'})
    assert [x['value'] for x in result['area']] == [value]
    assert result['area'][0]['raw_value'] == raw
    assert result['area'][0]['raw_text'] == text
    assert audit['observations'][0]['response_locator'] == 'response/line1'
    assert not result['area'][0]['can_publish']
    assert admit_value('area', value) == ([value], None)
    assert parse_text(text, {'id': 'page'}, {'provider': 'test', 'model_id': 'test'}) == (result, audit)


@pytest.mark.parametrize('raw', [
    '2或3平方米,四至:东至空地,南至宅,西至道路,北至宅',
    '2 3平方米,四至:东至空地,南至宅,西至道路,北至宅',
    '2,四至:东至空地,南至宅,西至道路,北至宅',
    '2平方米,四至:东至空地',
    '2平方米,四至:东至空地,南至宅,西至道路,北至宅，面积更正为3平方米',
    '2平方米,四至:东至空地,南至宅,西至道路,北至宅；改为3平方米',
    '2平方米；原面积3平方米',
])
def test_boundary_never_discards_another_area_or_incomplete_structure(raw):
    assert admit_value('area', raw)[0] == []


def test_different_physical_positions_keep_both_area_values():
    text = '土地面积2平方米,四至:东至空地,南至宅,西至道路,北至宅\n土地面积3平方米'
    result, _ = parse_text(text, {'id': 'page'}, {'provider': 'test', 'model_id': 'test'})
    assert {x['value'] for x in result['area']} == {'2', '3'}
    assert {x['response_locator'] for x in result['area']} == {'response/line1', 'response/line2'}
