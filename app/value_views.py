"""Versioned comparison views. Never change source facts, candidate admission or publication."""
import re

VERSION = 'field-value-views-v1'
IDENTIFIER_POLICY = 'balanced-year-brackets-v1'
ANONYMITY_POLICY = 'literal-placeholder-v1'
FULLWIDTH = str.maketrans({chr(n + 0xFEE0): chr(n) for n in range(0x21, 0x7F)})
PAIRS = {'(': ')', '（': '）', '[': ']', '〔': '〕', '【': '】'}
YEAR = re.compile(r'(?P<left>[（(\[〔【])(?P<year>\d{4})(?P<right>[）)\]〕】])')


def comparison_view(field, value):
    """Only identifier syntax has an equivalence protocol; all other fields stay literal."""
    original = '' if value is None else str(value)
    key = original
    if field in {'contract_no', 'original_contract_no'}:
        candidate = re.sub(r'\s+', '', original.translate(FULLWIDTH))
        matches = list(YEAR.finditer(candidate))
        # Exactly one balanced year token. Never repair digits, prefixes or serials.
        if len(matches) == 1 and PAIRS[matches[0]['left']] == matches[0]['right']:
            m = matches[0]
            key = candidate[:m.start()] + '〔' + m['year'] + '〕' + candidate[m.end():]
    return {'version': VERSION, 'field': field, 'original_value': original,
            'semantic_comparison_value': key, 'display_value': original,
            'score_comparison_value': key,
            'comparison_policy': IDENTIFIER_POLICY if field in {'contract_no', 'original_contract_no'} else 'literal-v1',
            'anonymity_policy': ANONYMITY_POLICY,
            'changes_source_fact': False, 'publication_allowed': False}


def source_key(row):
    p = row.get('parse_provenance') or {}
    replay = row.get('replay_source') or {}
    return (p.get('response_id') or replay.get('response_id'), row.get('source_page_id'),
            p.get('response_locator') or row.get('response_locator'), str(p.get('source_span')))


def conflict_diagnosis(field, rows):
    """A structural triage, not a claim that a model or the original is correct."""
    values = {str(r.get('value')) for r in rows if r.get('value')}
    semantic = {comparison_view(field, x)['score_comparison_value'] for x in values}
    sources = {source_key(r) for r in rows}
    roles = {r.get('text_role') for r in rows}
    category = 'not_a_value_conflict'
    if roles & {'crossed_out', 'alternative'}:
        category = 'source_alteration_or_explicit_alternative'
    elif len(values) > 1:
        if len(semantic) == 1:
            category = 'format_equivalent_under_versioned_protocol'
        elif len({re.sub(r'^[xX×*＊]+(?=[\u4e00-\u9fff])', 'PLACEHOLDER', x) for x in values}) == 1:
            category = 'possible_anonymous_prefix_disagreement_not_equivalent'
        elif any('原合同编号' in str(r.get('raw_text', '')) for r in rows) and field == 'contract_no':
            category = 'possible_current_original_role_mismatch_needs_boundary_review'
        elif any(len({r.get('value') for r in rows if source_key(r) == s}) > 1 for s in sources):
            category = 'same_source_multivalue_or_parser_disagreement'
        elif len({r.get('source_page_id') for r in rows}) > 1:
            category = 'cross_page_reading_disagreement'
        else:
            category = 'same_page_multiple_readings_position_not_proven'
    return {'rule_id': 'conflict-structural-triage-v2', 'category': category,
            'first_proven_layer': 'comparison_protocol' if category.startswith('format_equivalent') else 'candidate_evidence',
            'original_truth_determined': False, 'source_count': len(sources),
            'response_count': len({x[0] for x in sources}), 'candidate_count': len(rows),
            'duplicates_not_independent_evidence': len(rows) - len(sources),
            'automatic_value_selection': False, 'publication_allowed': False}
