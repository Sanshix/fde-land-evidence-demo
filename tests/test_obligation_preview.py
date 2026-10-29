from datetime import date

import pytest
from test_api import HEAD, new_doc, reviewed
from test_api import client as client_fixture
from test_domain import fields

from app import store
from app.obligations import contract_obligation

client = client_fixture
RATES = [{'执行起始日': '2000-01-01', '用途': '住宅', '一级': '1.25'}]


def test_period_scope_does_not_extend_expired_contract_or_claim_paid_zero():
    result = contract_obligation(fields(start='2020-07-01', end='2021-03-31', annual='120',
                                        annual_upper='壹佰贰拾元整'), RATES)
    assert [(x['year'], x['due']) for x in result['annual']] == [(2020, '60.00'), (2021, '30.00')]
    assert result['due'] == '90.00' and result['scope'] == 'stated_contract_period'
    assert result['paid'] is result['arrears'] is result['overpaid'] is None
    assert all(x['paid'] is x['arrears'] is None for x in result['annual'])
    assert not result['business_complete'] and not result['publication_allowed']


def test_floating_rounding_and_approved_relief_are_applied_per_year():
    f = fields(mode='随标准调整', area='2', use='住宅', grade='一级',
               start='2026-07-01', end='2027-06-30')
    relief = {'减免起始年度': 2026, '减免终止年度': 2026, '减免比例': '50%', '批准文号': 'SYN-RELIEF'}
    result = contract_obligation(f, RATES, [relief], as_of=date(2026, 9, 30))
    assert result['due'] == '0.63'  # 2 * 1.25 * 6/12 -> 1.25; 50% -> 0.63.
    assert result['annual'][0]['basis']['relief_approval'] == 'SYN-RELIEF'


@pytest.mark.parametrize('patch,unresolved', [
    ({'annual': None}, []), ({'annual': 'NaN'}, []), ({'annual': '-1'}, []),
    ({'mode': '未勾选'}, []), ({'start': '2026-02-30'}, []),
    ({'end': '2020-01-01'}, []), ({'annual_upper': '贰元整'}, []),
    ({'doc_type': '变更'}, []), ({}, ['annual']), ({}, ['annual_upper_conflict']),
])
def test_unknown_or_conflicting_financial_inputs_never_become_zero(patch, unresolved):
    with pytest.raises(ValueError):
        contract_obligation(fields(**patch), RATES, unresolved=unresolved)


def test_duplicate_rate_or_relief_is_not_arbitrarily_selected():
    f = fields(mode='随标准调整', grade='一级', use='住宅')
    with pytest.raises(ValueError, match='重复版本'):
        contract_obligation(f, RATES + RATES)
    relief = {'减免起始年度': 2026, '减免终止年度': 2026, '减免比例': '50%', '批准文号': 'SYN'}
    with pytest.raises(ValueError, match='多个减免'):
        contract_obligation(fields(), RATES, [relief, relief])


def test_upstream_area_material_gap_does_not_block_clear_own_period_arithmetic():
    result = contract_obligation(fields(), RATES, unresolved=['area_change_material_chain', 'payment_attribution'])
    assert result['due'] == '120.00' and not result['publication_allowed']


def test_fixed_formula_preserves_area_uncertainty_without_using_area():
    result = contract_obligation(fields(area='unknown'), RATES, unresolved=['area_crossout_adjudication'])
    assert result['due'] == '120.00' and 'area' not in result['calculation_dependencies']
    assert result['unresolved_items_retained'] == ['area_crossout_adjudication']
    assert not result['publication_allowed'] and result['paid'] is None


def test_approved_floating_formula_does_not_resolve_handwritten_annual_conflict():
    f = fields(mode='随标准调整', grade='一级', area='2', annual='7', annual_upper='捌元整')
    result = contract_obligation(f, RATES, unresolved=['annual_upper_conflict'])
    assert result['due'] == '2.50'
    assert result['unresolved_items_retained'] == ['annual_upper_conflict']
    assert not result['publication_allowed']
    with pytest.raises(ValueError):
        contract_obligation(f, RATES, unresolved=['area_crossout_adjudication'])


def test_month_precision_cutoff_never_manufactures_a_source_day():
    f = fields(start='2022-06-20', end='2025-05-31', annual='120', annual_upper='壹佰贰拾元整')
    result = contract_obligation(f, RATES, termination_month='2023-05')
    assert result['due'] == '120.00'
    assert [(r['year'], r['months']) for r in result['annual']] == [(2022, 7), (2023, 5)]
    assert result['termination']['day'] is None
    assert result['end'] == '2025-05-31' and not result['publication_allowed']
    same_month = contract_obligation(f, RATES, termination_month='2022-06')
    assert same_month['due'] == '10.00'


@pytest.mark.parametrize('month', ['2023-13', '2023', '2023-05-31', '2020-01'])
def test_invalid_or_pre_contract_cutoff_is_rejected(month):
    with pytest.raises(ValueError):
        contract_obligation(fields(), RATES, termination_month=month)


def test_due_only_api_preserves_pending_state_and_cannot_publish(client):
    doc, _ = new_doc(client)
    body = reviewed(doc)
    body.update(obligation_evidence_clear=True, obligation_reliefs_reviewed=True,
                confirm_payments=False, evidence_clear=False,
                unresolved_fields=['identity', 'payment_attribution'])
    response = client.post(f"/api/documents/{doc['id']}/obligation-preview", json=body, headers=HEAD)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['due'] == '120.00' and data['paid'] is data['arrears'] is None
    assert 'draft_hash' not in data and not data['publication_allowed']
    assert client.post(f"/api/documents/{doc['id']}/confirm", json=body, headers=HEAD).status_code == 400
    assert client.get('/api/ledger').json() == []
    with store.connection() as db:
        assert db.execute('select count(*) from publications').fetchone()[0] == 0
    assert client.get(f"/api/documents/{doc['id']}").json()['version'] == doc['version']


def test_obligation_api_requires_role_version_financial_review_and_citations(client):
    doc, _ = new_doc(client)
    body = reviewed(doc) | {'obligation_evidence_clear': True, 'obligation_reliefs_reviewed': True}
    path = f"/api/documents/{doc['id']}/obligation-preview"
    assert client.post(path, json=body).status_code == 403
    assert client.post(path, json=body | {'version': -1}, headers=HEAD).status_code == 409
    for patch in [{'obligation_evidence_clear': False}, {'obligation_reliefs_reviewed': False},
                  {'citations': {}}, {'unresolved_fields': ['annual']}, {'relief_ids': ['MISSING']},
                  {'termination_month': '2026-05'}]:
        assert client.post(path, json=body | patch, headers=HEAD).status_code == 400
    monthly = body | {'termination_month': '2026-05', 'termination_month_source_reviewed': True,
                      'citations': body['citations'] | {'termination_month': doc['pages'][0]['id']}}
    response = client.post(path, json=monthly, headers=HEAD)
    assert response.status_code == 200 and response.json()['due'] == '50.00'
    assert response.json()['termination']['day'] is None


def test_saved_financial_uncertainty_survives_omission_in_preview_and_draft_save(client):
    doc, _ = new_doc(client)
    body = reviewed(doc) | {'obligation_evidence_clear': True, 'obligation_reliefs_reviewed': True}
    pending = body | {'unresolved_fields': ['end_date_alteration'], 'evidence_clear': False}
    assert client.post(f"/api/documents/{doc['id']}/draft", json=pending, headers=HEAD).status_code == 200
    path = f"/api/documents/{doc['id']}/obligation-preview"
    assert client.post(path, json=body, headers=HEAD).status_code == 400
    # A legacy client omitting the key is not an adjudication of the source issue.
    assert client.post(f"/api/documents/{doc['id']}/draft", json=body, headers=HEAD).status_code == 200
    assert client.get(f"/api/documents/{doc['id']}").json()['draft']['unresolved_fields'] == ['end_date_alteration']
    assert client.post(path, json=body, headers=HEAD).status_code == 400


@pytest.mark.parametrize('address,status', [('测试街1号', 200), ('另一宗地街2号', 400)])
def test_obligation_relief_selection_must_still_match_current_contract(client, monkeypatch, address, status):
    from app import main

    doc, _ = new_doc(client)
    approval = {'批准文号': 'SYN-RELIEF-SCOPE', '坐落': address,
                '减免起始年度': 2026, '减免终止年度': 2026, '减免比例': '50%'}
    monkeypatch.setattr(main, 'SOURCE', {**main.SOURCE, 'reliefs': [approval]})
    body = reviewed(doc) | {'obligation_evidence_clear': True, 'obligation_reliefs_reviewed': True,
                            'relief_ids': [approval['批准文号']]}
    response = client.post(f"/api/documents/{doc['id']}/obligation-preview", json=body, headers=HEAD)
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()['due'] == '60.00' and response.json()['arrears'] is None
    else:
        assert '适用范围' in response.json()['detail']


def test_initial_obligation_choices_use_current_business_source(client, monkeypatch):
    from app import main

    doc, _ = new_doc(client)
    assert client.post(f"/api/documents/{doc['id']}/draft", json=reviewed(doc), headers=HEAD).status_code == 200
    approval = {'批准文号': 'SYN-CURRENT-RELIEF', '坐落': '测试街1号'}
    monkeypatch.setattr(main, 'active_source', lambda: {**main.SOURCE, 'reliefs': [approval]})
    result = client.get(f"/api/documents/{doc['id']}").json()
    assert result['obligation_matches']['reliefs'] == [approval]
