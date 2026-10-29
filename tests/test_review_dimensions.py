import copy

import pytest

from app.review_dimensions import material_reference, receipt_groups, review_dimensions


def test_exact_reference_requires_parcel_and_preserves_roles_and_zeroes():
    fields = {'new': {'original_contract_no': '文租（2020）第001号', 'parcel_no': '8-3-001'},
              'old': {'contract_no': '文租〔2020〕第001号', 'parcel_no': '8-3-001'},
              'other': {'contract_no': '文租〔2020〕第01号', 'parcel_no': '8-3-001'}}
    before = copy.deepcopy(fields)
    relation = material_reference('new', fields)
    assert relation['same_parcel_candidates'] == ['old'] and not relation['adopted']
    assert fields == before
    fields['old']['parcel_no'] = '8-3-002'
    assert material_reference('new', fields)['status'] == 'parcel_disagrees_or_unknown'
    fields['other']['contract_no'] = fields['old']['contract_no']
    assert material_reference('new', fields)['status'] == 'ambiguous_reference'


def test_receipts_group_identical_rows_without_deleting_sources_or_choosing_larger_amount():
    rows = [{'序号': 1, '票据号': '001', '金额（元）': 2},
            {'序号': 2, '票据号': '001', '金额（元）': 2},
            {'序号': 3, '票据号': '01', '金额（元）': 2}]
    original = copy.deepcopy(rows)
    groups = receipt_groups(rows)
    assert len(groups) == 2 and groups[0]['source_row_ids'] == ['1', '2']
    assert groups[0]['identical_duplicate_rows'] == 1 and rows == original
    rows[1]['金额（元）'] = 3
    group = receipt_groups(rows)[0]
    assert group['conflicting_receipt_facts'] and group['allocation_count'] == 0


def test_unresolved_number_is_not_evidence_of_missing_original_material():
    fields = {'new': {'original_contract_no': '甲租(2020)001号', 'parcel_no': '8-3-001'},
              'old': {'contract_no': None, 'parcel_no': '8-3-001'}}
    relation = material_reference('new', fields, {'old': ['甲租〔2020〕001号', '其他读数']})
    assert relation['status'] == 'unadopted_machine_reference_candidate'
    assert relation['unadopted_machine_number_candidates'] == ['old']
    assert not relation['material_absence_proven'] and not relation['adopted']
    relation = material_reference('new', fields)
    assert relation['status'] == 'reference_unresolved_available_same_parcel_material'
    assert relation['number_candidates'] == []
    fields['old']['parcel_no'] = '8-3-002'
    assert material_reference('new', fields)['status'] == 'reference_not_resolved_from_available_fields'


def test_anonymous_number_prefix_does_not_automatically_create_reference_link():
    fields = {'new': {'original_contract_no': 'xx租(2020)001号', 'parcel_no': '8-3-001'},
              'old': {'contract_no': '××租(2020)001号', 'parcel_no': '8-3-001'}}
    relation = material_reference('new', fields, {'old': ['××租(2020)001号']})
    assert relation['number_candidates'] == relation['unadopted_machine_number_candidates'] == []
    assert relation['available_same_parcel_documents'] == ['old'] and not relation['adopted']


def test_missing_receipt_ids_do_not_collapse_and_zero_identifier_survives():
    groups = receipt_groups([{'序号': 1, '票据号': None}, {'序号': 2, '票据号': None},
                             {'序号': 3, '票据号': 0}])
    assert len(groups) == 3 and '0' in {x['receipt'] for x in groups}


def test_formula_with_or_without_weak_matches_never_implies_paid_zero_or_publishable():
    for weak in [[], [{'序号': 1}]]:
        result = review_dimensions(document_state='needs_human', unresolved=['missing_signature'],
            formula={'due': '20.00'}, weak_rows=weak, material={'status': 'missing_reference'})
        assert result['due'] == '20.00' and result['paid'] is result['arrears'] is None
        assert result['retained_unresolved'] == ['missing_signature']
        assert not result['new_publication_allowed'] and result['publication_status'] == 'not_published'
    unknown = review_dimensions(document_state='needs_human', unresolved=[], formula=None,
                                weak_rows=[], material={'status': 'not_referenced'})
    assert unknown['due'] is None and unknown['paid'] is None


@pytest.mark.parametrize('due', [None, 'NaN', 'Infinity', '-1'])
def test_unknown_invalid_formula_amount_does_not_become_computable(due):
    result = review_dimensions(document_state='needs_human', unresolved=[], formula={'due': due},
                               weak_rows=[], material={'status': 'not_referenced'})
    assert result['formula_status'] == 'formula_undetermined' and result['due'] is None
