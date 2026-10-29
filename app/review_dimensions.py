"""Read-only material/payment review dimensions; never infer publication from arithmetic."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .domain import cadastre
from .field_semantics import canonical_identifier

VERSION = 'review-dimensions-v2'


def material_reference(name, fields_by_name, machine_contract_numbers=None):
    own = fields_by_name[name]
    reference = canonical_identifier(own.get('original_contract_no'))
    matches = sorted(n for n, f in fields_by_name.items() if n != name and reference
                     and canonical_identifier(f.get('contract_no')) == reference)
    parcel = cadastre(own.get('parcel_no'))
    agreed = [n for n in matches if parcel and parcel == cadastre(fields_by_name[n].get('parcel_no'))]
    available = sorted(n for n, f in fields_by_name.items() if n != name and parcel
                       and parcel == cadastre(f.get('parcel_no')))
    machine = sorted(n for n, numbers in (machine_contract_numbers or {}).items() if n != name and reference
                     and any(canonical_identifier(v) == reference for v in numbers))
    if not reference:
        status = 'not_referenced'
    elif not matches:
        status = ('unadopted_machine_reference_candidate' if machine else
                  'reference_unresolved_available_same_parcel_material' if available else
                  'reference_not_resolved_from_available_fields')
    elif len(matches) > 1:
        status = 'ambiguous_reference'
    elif len(agreed) != 1:
        status = 'parcel_disagrees_or_unknown'
    else:
        status = 'unique_reference_and_parcel_candidate_not_adopted'
    return {'rule_id': 'material-reference-exact-v2', 'original_reference': own.get('original_contract_no'),
            'number_candidates': matches, 'same_parcel_candidates': agreed, 'status': status,
            'available_same_parcel_documents': available, 'unadopted_machine_number_candidates': machine,
            'material_absence_proven': False,
            'adopted': False, 'legal_effect_determined': False}


def receipt_groups(rows):
    """Group source rows by literal receipt identity; never merge unequal facts or strip zeroes."""
    groups = defaultdict(list)
    for row in rows:
        # Empty receipt identifiers cannot constitute one shared receipt.
        raw = row.get('票据号')
        literal = '' if raw is None else str(raw).strip()
        key = literal or 'missing-id-row:' + str(row['序号'])
        groups[key].append(row)
    result = []
    for receipt, records in sorted(groups.items()):
        facts = [{k: v for k, v in r.items() if k != '序号'} for r in records]
        same = all(f == facts[0] for f in facts)
        result.append({'receipt': receipt, 'source_row_ids': [str(r['序号']) for r in records],
                       'source_rows': records, 'identical_duplicate_rows': len(records) - 1 if same else 0,
                       'conflicting_receipt_facts': not same,
                       'automatic_assignment': False, 'allocation_count': 0})
    return result


def review_dimensions(*, document_state, unresolved, formula, weak_rows, material, publication=None):
    """Inputs are evidence states, not an answer key. Unknown payment totals remain null."""
    is_published = publication is not None and document_state == 'published'
    calculation = formula or {}
    try:
        amount = Decimal(str(calculation.get('due')))
        formula_known = amount.is_finite() and amount >= 0
    except InvalidOperation:
        formula_known = False
    return {'version': VERSION, 'import_status': 'imported',
            'evidence_review_status': 'unresolved_items_retained' if unresolved else 'no_saved_unresolved_items',
            'material_status': material['status'],
            'formula_status': 'conditional_formula_computable' if formula_known else 'formula_undetermined',
            'payment_status': 'published_allocation_snapshot' if is_published else 'attribution_unresolved',
            'weak_receipt_source_rows': len(weak_rows),
            'due': publication['due'] if is_published else calculation.get('due') if formula_known else None,
            'paid': publication['paid'] if is_published else None,
            'arrears': publication['arrears'] if is_published else None,
            'publication_status': 'existing_test_review_snapshot' if is_published else 'not_published',
            'new_publication_allowed': False, 'human_signature_claimed': False,
            'retained_unresolved': list(unresolved),
            'rule_id': 'formula-is-not-business-completion-v1'}
