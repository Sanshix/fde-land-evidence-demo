"""Local syntax admission. Unknown text stays evidence, never a manufactured fact.

No answers, model calls, source-image inference, or publication decisions live here.
Response locators describe OCR text, not verified pixels in the original contract.
"""

import hashlib
import json
import re
import unicodedata
from datetime import date

from .domain import FIELDS
from .extraction import CN_DATE, cn_date, prose_candidates

VERSION = 'deterministic-admission-v7-label-segment-spans'
NUMBER = r'[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?'
DATE = re.compile(CN_DATE + r'|(\d{4})[./-](\d{1,2})[./-](\d{1,2})')
UNIT = r'(?:平方米|㎡|m\s*(?:²|2|\^\s*\{?2\}?))'

# Longest complete labels consume roles and units before the value is read.
LABELS = {
    '原合同编号': 'original_contract_no', '合同编号': 'contract_no', '合同号': 'contract_no',
    '原承租人': 'previous_tenant', '变更前承租人': 'previous_tenant',
    '变更后承租人': 'tenant', '新承租人': 'tenant', '承租人': 'tenant', '承租方': 'tenant',
    '出租人': 'lessor', '身份证号码': 'identity', '证件号码': 'identity', '证件号': 'identity',
    '营业执照号': 'identity', '统一社会信用代码': 'identity',
    '联系电话号码': 'phone', '联系电话': 'phone', '住所': 'residence',
    '不动产单元号': 'parcel_no', '宗地号': 'parcel_no', '地号': 'parcel_no',
    '土地坐落': 'address', '坐落': 'address', '宗地面积': 'area', '土地面积': 'area', '面积': 'area',
    '土地用途': 'use', '用途': 'use', '土地级别': 'grade',
    '租赁起始日期': 'start', '租赁起始日': 'start', '租赁终止日期': 'end', '租赁终止日': 'end',
    '租赁期限': 'term', '签订日期': 'signed', '合同类型': 'doc_type', '计租方式': 'mode',
    '年租金(小写)': 'annual', '年租金小写': 'annual', '年租金(大写)': 'annual_upper',
    '年租金大写': 'annual_upper', '年租金标准': 'rate_standard', '年租金': 'annual',
}
LABEL_PATTERN = re.compile('|'.join(r'\s*'.join(re.escape(c) for c in label)
                                   for label in sorted(LABELS, key=len, reverse=True)))


class CandidateMap(dict):
    def __init__(self, *args, diagnostics=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.diagnostics = diagnostics or {'parser_version': VERSION, 'observations': []}


def response_identity(raw, profile):
    payload = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True)
    return profile.get('response_id') or 'sha256:' + hashlib.sha256(payload.encode()).hexdigest()


def format_text(raw):
    """Only known visual wrappers, never substitution of an uncertain character."""
    text = unicodedata.normalize('NFKC', str(raw)).strip()
    text = re.sub(r'<\|LOC_\d+\|>', '', text)
    text = re.sub(r'\*\*([^*\n]+)\*\*', r'\1', text)
    text = re.sub(r'^#{1,6}\s+', '', text)
    # A displayed square-metre formula is syntax. Other formulas remain unresolved.
    text = re.sub(r'\$\s*[mM]\s*\^\s*\{+\s*2\s*\}+\s*\$', '平方米', text)
    return text


def date_values(text):
    values, ranges = [], []
    for match in DATE.finditer(text):
        groups = match.groups()
        if groups[0] is not None:
            chinese = re.fullmatch(CN_DATE, match.group())
            value = cn_date(chinese)
        else:
            try:
                value = date(*map(int, groups[3:])).isoformat()
            except ValueError:
                value = None
        if value is None:
            return [], 'invalid_date'
        values.append(value)
        ranges.append(match.span())
    remainder = DATE.sub('', text)
    remainder = re.sub(r'签订日期\s*[:：]?', '', remainder).strip(' ,，;；:：|\t\n')
    if not values or remainder:
        return [], 'date_syntax_unresolved'
    if len(set(values)) != 1:
        return [], 'multiple_dates'
    return [values[0]], None


def admit_value(key, raw):
    """Returns (values, rejection). A rejected value is never a business candidate."""
    text = format_text(raw).strip().strip('：:；;。')
    if not text or re.search(r'【.*?】|待填写|待确认|无法确定|无法辨认', text):
        return [], 'missing_or_unreadable'
    if key in {'area', 'annual', 'rate_standard'}:
        # Only a complete value grammar is accepted. Unit digits are never scanned.
        if key == 'area':
            # A complete value plus unit followed by the explicit 四至 clause
            # ends the area value. Keep the whole response in the observation.
            boundary = re.fullmatch(rf'({NUMBER}\s*{UNIT})\s*[,，;；]\s*四至\s*[:：](.+)', text, re.I)
            if boundary:
                surroundings = boundary.group(2)
                if (all(word in surroundings for word in ('东至', '南至', '西至', '北至'))
                        and not re.search(r'面积|涂改|改为|更正|划去|作废|替换|另计', surroundings)):
                    text = boundary.group(1)
            pattern = rf'(?:\(\s*{UNIT}\s*\)\s*)?({NUMBER})\s*(?:{UNIT})?'
        elif key == 'annual':
            pattern = rf'(?:人民币\s*)?({NUMBER})\s*元?'
        else:
            pattern = rf'({NUMBER})\s*(?:元\s*(?:/\s*{UNIT}\s*(?:[·/]年)?)?)?'
        match = re.fullmatch(pattern, text, flags=re.I)
        if not match:
            return [], 'numeric_value_not_unique_or_invalid'
        return [match.group(1).replace(',', '').replace('−', '-')], None
    if key == 'annual_upper' and re.fullmatch(rf'{NUMBER}\s*元\s*/\s*(?:m2|平方米|㎡)(?:[·/]年)?', text, re.I):
        return [], 'numeric_unit_rate_not_uppercase_amount'
    if key in {'start', 'end', 'signed'}:
        return date_values(text)
    if re.search(r'\$|\\[a-zA-Z]+|\^\{|~~|<del>|</?s>', text):
        return [], 'unknown_format_or_alteration'
    if key == 'identity' and not re.fullmatch(r'[A-Za-z0-9]{15,20}', text):
        return [], 'invalid_identifier_syntax'
    if key == 'phone' and not re.fullmatch(r'[+\d][\d ()-]{5,24}', text):
        return [], 'invalid_phone_syntax'
    # Only the explicit GB/F unit-number family; never pad digits or alter old parcel codes.
    if key == 'parcel_no' and re.fullmatch(r'[0-9]+GB[0-9]+F[0-9]+', text):
        if not re.fullmatch(r'[0-9]{12}GB[0-9]{5}F[0-9]{8}', text):
            return [], 'invalid_cadastral_unit_grammar'
    if key in {'contract_no', 'original_contract_no', 'parcel_no'}:
        if re.search(r'[:：]|合同编号|承租人|联系电话|\s(?:或|至)\s', text):
            return [], 'identifier_boundary_unresolved'
    if key in {'use', 'grade', 'mode', 'doc_type'}:
        from .governance import normalize
        values = normalize(key, text)
        if key == 'use' and any(v not in {'住宅', '商服', '工业', '仓储'} for v in values):
            return [], 'invalid_enum'
        if key == 'grade' and any(v not in {'一级', '二级', '三级', '四级'} for v in values):
            return [], 'invalid_enum'
        return values, None if values else 'unselected_or_unresolved'
    return [text], None


def trace(key, raw, values, reason, locator, response_id, *, scope='line', span=None):
    return {'field': key, 'raw_value': raw, 'normalized_values': values, 'rule_version': VERSION,
            'response_id': response_id, 'response_locator': locator, 'source_span': span,
            'span_scope': scope, 'candidate_status': 'valid' if values else 'needs_read',
            'rejection_reason': reason, 'original_location_verified': False}


def labeled_segments(raw, *, with_spans=False):
    """Recognize a line starting with a schema label; split only explicit inline boundaries."""
    text = format_text(raw)
    first = LABEL_PATTERN.match(text)
    if not first:
        return []
    matches = [first]
    for match in LABEL_PATTERN.finditer(text, first.end()):
        # A label-like substring inside a name is not a field boundary.
        separated = match.start() > 0 and (text[match.start()-1].isspace() or text[match.start()-1] in '|;；')
        previous_key = LABELS[re.sub(r'\s+', '', matches[-1].group())]
        next_key = LABELS[re.sub(r'\s+', '', match.group())]
        typed_contact = False
        if previous_key == 'identity' and next_key == 'phone':
            left = text[matches[-1].end():match.start()].strip().lstrip(':：').strip()
            left = re.sub(r'^\(\s*营业执照号\s*\)\s*', '', left).lstrip(':：').strip()
            right = text[match.end():].strip().lstrip(':：').strip()
            typed_contact = bool(re.fullmatch(r'[A-Za-z0-9]{15,20}', left)
                                 and re.fullmatch(r'\d{11}', right))
        if separated or typed_contact:
            matches.append(match)
    # Map only direct, character-wise NFKC. Removed wrappers or composed character
    # sequences have no proven offset mapping and deliberately retain a coarse scope.
    chunks = [unicodedata.normalize('NFKC', char) for char in str(raw)]
    expanded = ''.join(chunks)
    direct = expanded == unicodedata.normalize('NFKC', str(raw)) and expanded.strip() == text
    indices = [index for index, chunk in enumerate(chunks) for _ in chunk] if direct else []
    leading = len(expanded) - len(expanded.lstrip())
    segments = []
    for index, match in enumerate(matches):
        label = re.sub(r'\s+', '', match.group())
        key = LABELS[label]
        end = matches[index+1].start() if index+1 < len(matches) else len(text)
        value = text[match.end():end].strip()
        if key in {'tenant', 'lessor', 'identity'}:
            role = {'tenant': r'乙方|签字|签字或盖章', 'lessor': r'甲方', 'identity': r'营业执照号'}[key]
            value = re.sub(r'^\(\s*(?:' + role + r')\s*\)\s*', '', value)
        if key == 'area':
            value = re.sub(r'^\(\s*' + UNIT + r'\s*\)\s*', '', value, flags=re.I)
        value = value.lstrip(':：\t ').rstrip(' |;；')
        span = ([indices[leading + match.start()], indices[leading + end - 1] + 1]
                if indices and end > match.start() else None)
        segments.append((key, value, span) if with_spans else (key, value))
    return segments


def source_value(raw_text, value):
    """Recover an exact source slice for a normalized value when mapping is unique."""
    chunks = [unicodedata.normalize('NFKC', char) for char in raw_text]
    expanded = ''.join(chunks)
    normalized = unicodedata.normalize('NFKC', str(value))
    if not normalized or expanded.count(normalized) != 1:
        return value
    indices = [index for index, chunk in enumerate(chunks) for _ in chunk]
    start = expanded.index(normalized)
    return raw_text[indices[start]:indices[start + len(normalized) - 1] + 1]


def parse_lines(lines, page, profile, *, response_id=None, kind='ocr', native=False):
    """Labels and clauses share one source arbitration; no independent-evidence voting."""
    response_id = response_id or response_identity([line['text'] for line in lines], profile)
    diagnostics = {'parser_version': VERSION, 'response_id': response_id, 'observations': [],
                   'parser_disagreements': [], 'raw_output_retained': True}
    out = CandidateMap({key: [] for key in FIELDS}, diagnostics=diagnostics)
    claimed = {}

    def add(key, raw_value, raw_text, line, parser, *, already_typed=False, label_span=None):
        locator = line.get('locator')
        raw_value = source_value(raw_text, raw_value)
        values, reason = admit_value(key, raw_value)
        exact = label_span is not None and locator is not None and raw_text == line['text']
        observation = trace(key, raw_value, values, reason, locator, response_id,
                            scope='ocr_label_segment' if exact else 'line',
                            span=label_span if exact else [0, len(line['text'])] if raw_text == line['text'] else None)
        if exact:
            observation['source_span_text'] = line['text'][label_span[0]:label_span[1]]
            observation['source_span_meaning'] = 'label_and_value_in_original_ocr_line_not_image_pixels'
        observation.update(parser=parser, raw_text=raw_text)
        diagnostics['observations'].append(observation)
        for value in values:
            candidate = {'value': value, 'raw_value': raw_value, 'raw_text': raw_text,
                         'model': profile['model_id'], 'provider': profile['provider'],
                         'source_page_id': page['id'], 'response_locator': None if native else locator,
                         'source_locator': locator if native else None, 'bbox': line.get('box'),
                         'score': line.get('score'), 'score_meaning': 'ocr_line_score' if 'score' in line else 'not_provided',
                         'kind': kind, 'verified': False, 'can_publish': False,
                         'evidence_status': 'line_region_needs_human' if line.get('box') else 'missing_region',
                         'reason': '结构与语法准入；原文和业务含义仍须核验', 'parse_provenance': observation,
                         'text_role': line.get('text_role', 'unspecified')}
            out[key].append(candidate)

    for index, line in enumerate(lines):
        if line.get('formula') is not None or line.get('hidden') or line.get('tracked_change'):
            continue
        for key, value, label_span in labeled_segments(line['text'], with_spans=True):
            loc = line.get('locator')
            raw_text = line['text']
            claimed.setdefault(loc, set()).add(key)
            selected_line = line
            if not value and index + 1 < len(lines):
                following = lines[index+1]
                if (following.get('formula') is None and not following.get('hidden')
                        and not following.get('tracked_change') and following['text'].strip()
                        and not labeled_segments(following['text'])):
                    value = following.get('display_value', following['text'])
                    if following.get('display_status') == 'date_interpretation_check_epoch':
                        value = value.split('T')[0]
                    claimed.setdefault(following.get('locator'), set()).add(key)
                    raw_text += '\n' + following['text']
                    # Text adjacency is evidence of response structure, never of pixel geometry.
                    selected_line = {**line, 'locator': f"{loc} + {following.get('locator')}", 'box': None}
                    label_span = None  # The value comes from a different OCR line.
            if key == 'previous_tenant':
                diagnostics['observations'].append(trace(key, value, [], 'previous_role_not_current', loc, response_id))
                continue
            if key == 'term':
                matches = list(DATE.finditer(value))
                if (len(matches) == 2 and not value[:matches[0].start()].strip()
                        and not value[matches[-1].end():].strip() and re.fullmatch(r'\s*(?:起)?\s*(?:至|到|—|~|～)\s*',
                                                       value[matches[0].end():matches[1].start()])):
                    for field, match in zip(('start', 'end'), matches, strict=True):
                        add(field, match.group(), raw_text, selected_line, 'labeled_term', label_span=label_span)
                    claimed[loc].update({'start', 'end'})
                else:
                    diagnostics['observations'].append(trace(key, value, [], 'term_unresolved', loc, response_id))
                continue
            add(key, value, raw_text, selected_line, 'complete_label', label_span=label_span)

    # Preserve supported clause recognition, but never re-interpret a claimed field position.
    prose = prose_candidates(lines, page['id'], profile['model_id'], profile['provider'], kind)
    for key, rows in prose.items():
        for row in rows:
            loc = row.get('response_locator')
            roles = claimed.get(loc, set()) if loc is not None else set()
            if key in roles or (key == 'contract_no' and 'original_contract_no' in roles):
                diagnostics['parser_disagreements'].append({'field': key, 'response_locator': loc,
                    'clause_value': row['value'], 'decision': 'label_owns_same_position'})
                continue
            matching = [line for line in lines if line.get('locator') == loc] if loc else []
            line = matching[0] if len(matching) == 1 else {'text': row['raw_text'], 'locator': None}
            # The old parser used first-six-character matching; require the complete snippet.
            if re.sub(r'\s+', '', row['raw_text']) not in re.sub(r'\s+', '', line['text']):
                line = {'text': row['raw_text'], 'locator': None}
            add(key, row['value'], row['raw_text'], line, 'supported_clause')
    return out, diagnostics


def parse_text(text, page, profile):
    lines, seen, markers, repeated = [], set(), 0, 0
    for index, raw in enumerate(text.splitlines(), 1):
        clean, count = re.subn(r'<\|LOC_\d+\|>', '', raw)
        markers += count
        if clean in seen and clean.strip():
            repeated += 1
        # Equal response text at different positions is still distinct evidence.
        # Value comparison may deduplicate values; extraction must not erase a label.
        seen.add(clean)
        lines.append({'text': raw, 'locator': f'response/line{index}'})
    candidates, diagnostics = parse_lines(lines, page, profile, response_id=response_identity(text, profile))
    diagnostics.update(location_markers_removed_from_extraction=markers,
                       duplicate_lines_excluded_from_extraction=0,
                       duplicate_lines_observed=repeated)
    return candidates, diagnostics
