import pytest

from app.deterministic_parser import admit_value, parse_text
from app.extraction import prose_candidates


def read(text):
    return parse_text(text, {'id': 'p'}, {'model_id': 'test', 'provider': 'test'})[0]


@pytest.mark.parametrize('text,key,expected', [
    ('承租人（乙方）：甲公司（分公司）', 'tenant', ['甲公司(分公司)']),
    ('土地面积（m²）307.95', 'area', ['307.95']),
    ('土地面积：2㎡', 'area', ['2']),
    ('签订日期：2016年4月26日 签订日期：2016年4月26日', 'signed', ['2016-04-26']),
    ('证件号码：990524196008035852 联系电话：10048046937', 'identity', ['990524196008035852']),
])
def test_known_boundaries(text, key, expected):
    assert list(dict.fromkeys(c['value'] for c in read(text)[key])) == expected


@pytest.mark.parametrize('raw', ['307.95 2', '307.95或308.95'])
def test_ambiguous_numbers_rejected(raw):
    assert admit_value('area', raw)[0] == []


def test_conflicting_dates_rejected():
    assert admit_value('signed', '2016年4月26日 2016年4月27日')[0] == []


def test_term_suffix_not_silently_ignored():
    result = read('租赁期限：2010-07-01至2015-06-30 或另行约定')
    assert not result['start'] and not result['end']


def test_exact_locator_not_prefix():
    lines = [{'text': '签订日期：2001年1月1日', 'locator': 'a'},
             {'text': '签订日期：2002年2月2日', 'locator': 'b'}]
    rows = prose_candidates(lines, 'p', 'm', 'v')['signed']
    assert [(r['value'], r['response_locator']) for r in rows] == [('2001-01-01', 'a'), ('2002-02-02', 'b')]
