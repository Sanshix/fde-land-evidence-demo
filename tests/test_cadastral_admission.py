import json

import pytest
from test_api import client as client_fixture
from test_pr_acceptance import batch_publish, batch_rows, candidate

from app import store
from app.deterministic_parser import admit_value, parse_text
from app.triage import source_admission_blockers

client = client_fixture
GOOD = '000000008006GB00184F00065084'
BAD = '00000008006GB00184F00065084'


@pytest.mark.parametrize('value', [GOOD, '09-03-0116', '9-3-116', '7.3-100'])
def test_valid_unit_and_old_parcel_values_preserve_all_digits(value):
    assert admit_value('parcel_no', value) == ([value], None)
    assert admit_value('parcel_no', admit_value('parcel_no', value)[0][0]) == ([value], None)


def test_invalid_unit_is_retained_as_evidence_not_padded_or_admitted():
    candidates, diagnostics = parse_text('不动产单元号：' + BAD, {'id': 'p'},
                                          {'model_id': 'synthetic', 'provider': 'test'})
    assert candidates['parcel_no'] == []
    obs = diagnostics['observations'][0]
    assert obs['raw_value'] == BAD and obs['candidate_status'] == 'needs_read'
    assert obs['rejection_reason'] == 'invalid_cadastral_unit_grammar'
    assert obs['response_id'] and obs['response_locator']
    assert admit_value('parcel_no', BAD)[0] == []


def test_two_valid_different_units_remain_two_candidates():
    other = '000000008006GB00185F00065084'
    candidates, _ = parse_text('不动产单元号：' + GOOD + '\n不动产单元号：' + other,
                                {'id': 'p'}, {'model_id': 'synthetic', 'provider': 'test'})
    assert {x['value'] for x in candidates['parcel_no']} == {GOOD, other}


@pytest.mark.parametrize('reading', [
    {'extraction_diagnostics': {'observations': [{'field': 'parcel_no', 'raw_value': BAD,
                          'rejection_reason': 'invalid_cadastral_unit_grammar'}]}},
    {'raw_response': {'fields': [{'field': 'parcel_no', 'value': BAD}]}},
    {'raw_response': {'fields': {'parcel_no': {'value': BAD}}}},
])
def test_unique_valid_value_cannot_erase_source_rejection_at_batch_commit(client, reading):
    doc = candidate(client, 'synthetic-admission', 'white')
    with store.connection() as db:
        result = json.loads(db.execute('select result from documents where id=?', (doc['id'],)).fetchone()[0])
        result['primary'] = reading
        db.execute('update documents set result=? where id=?', (store.dumps(result), doc['id']))
    rows = batch_rows(client)
    row = next(x for x in rows if x['id'] == doc['id'])
    assert not row['eligible'] and '不合语法' in '；'.join(row['reasons'])
    response = batch_publish(client, [row])
    assert response.status_code == 200 and response.json()['published'] == 0
    assert client.get('/api/ledger').json() == []


def test_legacy_absent_diagnostics_and_valid_structured_values_are_not_new_risks():
    assert source_admission_blockers({'primary': None, 'raw_response': 'legacy text'}) == []
    assert source_admission_blockers({'primary': {'raw_response': {'fields': [
        {'field': 'parcel_no', 'value': GOOD}, {'field': 'tenant', 'value': BAD}]}}}) == []
