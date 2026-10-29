"""Offline template development only; production never reads answer files."""
import argparse
import csv
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.identifier_templates import parts  # noqa: E402


def derive(path):
    groups = defaultdict(list)
    for row in csv.DictReader(path.open(encoding='utf-8-sig')):
        match = parts(row['合同编号'])
        if match:
            groups[(match['prefix'], match['suffix'].startswith('第'))].append(match)
    rules = []
    for (prefix, di), rows in sorted(groups.items()):
        styles = {m['left'] + m['right'] for m in rows}
        rules.append({'id': 'sample-number-' + str(len(rules) + 1), 'prefix': prefix,
                      'serial_has_di': di, 'observed_years': sorted({int(m['year']) for m in rows}),
                      'brackets': next(iter(styles)) if len(styles) == 1 else None,
                      'enabled': len(styles) == 1, 'support_count': len(rows)})
    return {'version': 'exercise-number-format-v1', 'authority': 'sample_inferred_not_department_approved',
            'source_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'scope': 'Exact simulated prefix and observed years only; no serial-number lookup.', 'rules': rules}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--answers', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(derive(args.answers), ensure_ascii=False, indent=2) + '\n')
