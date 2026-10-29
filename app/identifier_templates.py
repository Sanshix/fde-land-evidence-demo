"""Versioned sample-derived formatting, separate from OCR truth and acceptance."""
import json
import re
from pathlib import Path

PAIRS = {'（': '）', '(': ')', '〔': '〕', '[': ']', '【': '】'}
PATTERN = re.compile(r'(?P<prefix>[^()（）〔〕\[\]【】\s]+)(?P<left>[（(〔\[【])(?P<year>[0-9]{4})(?P<right>[）)〕\]】])(?P<suffix>第?[0-9]+号)')
CONFIG = Path(__file__).resolve().parents[1] / 'config/identifier-templates.json'


def parts(value):
    match = PATTERN.fullmatch(value)
    return match if match and PAIRS[match['left']] == match['right'] else None


def comparison_key(value):
    """Balanced year brackets only; serials, prefixes and all other characters stay exact."""
    match = parts(value)
    return (match['prefix'] + '〔' + match['year'] + '〕' + match['suffix']) if match else value


def describe(value, config=None):
    config = config if config is not None else json.loads(CONFIG.read_text())
    result = {'ocr_value': value, 'comparison_key': comparison_key(value),
              'normalized_value': None, 'status': 'no_applicable_template',
              'human_verified': False, 'basis': None}
    match = parts(value)
    if not match:
        return result
    rules = [r for r in config['rules'] if r['prefix'] == match['prefix']
             and int(match['year']) in r['observed_years'] and r['enabled']
             and bool(match['suffix'].startswith('第')) == r['serial_has_di']]
    if len(rules) != 1:
        return result
    rule = rules[0]
    result.update(normalized_value=match['prefix'] + rule['brackets'][0] + match['year']
                  + rule['brackets'][1] + match['suffix'], status='sample_template_unverified',
                  basis={'rule_id': rule['id'], 'version': config['version'],
                         'authority': config['authority'], 'source_sha256': config['source_sha256']})
    return result
