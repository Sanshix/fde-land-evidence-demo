"""Offline replay of exactly five frozen documents. Never invokes a provider or writes a DB."""

import argparse
import hashlib
import json
import socket
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--code-root', type=Path, required=True)
    parser.add_argument('--documents', nargs=5, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if len(set(args.documents)) != 5:
        parser.error('exactly five distinct documents are required')
    if args.output.exists():
        parser.error('output must be new; old runs are immutable')
    # Fail closed even if a future parser accidentally attempts network I/O.
    def offline(*_args, **_kwargs):
        raise RuntimeError('network forbidden in frozen replay')
    socket.socket.connect = offline
    socket.create_connection = offline
    sys.path.insert(0, str(args.code_root.resolve()))
    from app.domain import FIELDS
    from app.governance import parse_ocr_text
    from app.ocr_layout import baidu_candidates
    from app.providers import parse_reply
    try:
        from app.field_adjudication import apply_adjudication
    except ImportError:
        def apply_adjudication(result):
            return result
    try:
        from app.deterministic_parser import review_warnings
    except ImportError:
        def review_warnings(*_args):
            return []  # Frozen baseline predates admission diagnostics.

    documents = json.loads((args.source / 'documents.json').read_text())
    selected = [d for d in documents if d['name'] in args.documents]
    if {d['name'] for d in selected} != set(args.documents):
        parser.error('requested documents missing from source manifest')
    args.output.mkdir(parents=True)
    (args.output / 'documents.json').write_text(json.dumps(selected, ensure_ascii=False, indent=2))
    hashes, counts = {}, {}
    for doc in selected:
        source = args.source / 'recognition' / doc['name'] / 'result.json'
        hashes[str(source.resolve())] = hashlib.sha256(source.read_bytes()).hexdigest()
        result = json.loads(source.read_text())
        candidates = {key: [] for key in FIELDS}
        stages, skipped, warnings = [], [], []
        for page in result['pages']:
            for index, stage in enumerate(page['stages']):
                if stage.get('status') != 'completed':
                    skipped.append({'page_id': page['id'], 'stage': index, 'status': stage.get('status')})
                    continue
                raw = stage.get('raw_response')
                if not isinstance(raw, dict):
                    raise ValueError(f'{doc["name"]}/{page["id"]}/{index}: completed response absent')
                profile = {'provider': stage['provider'], 'model_id': stage['requested_model']}
                response_hash = hashlib.sha256(json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                profile['response_id'] = stage.get('response_id') or response_hash
                region = stage.get('region') or [0, 0, page['quality']['width'], page['quality']['height']]
                if 'text' in raw:
                    group, diagnostics = parse_ocr_text(raw['text'], page, profile)
                elif 'pages' in raw:
                    group, diagnostics = baidu_candidates(raw, page, profile, region)
                elif 'fields' in raw:
                    group = parse_reply(raw, page, region, profile,
                                        'context_suggestion' if stage.get('stage') == 'context' else 'ocr')
                    diagnostics = getattr(group, 'diagnostics', {})
                else:
                    raise ValueError(f'unhandled frozen response: {list(raw)}')
                for key, rows in group.items():
                    for row in rows:
                        row['replay_source'] = {'response_id': profile['response_id'], 'sha256': response_hash,
                                                'page_id': page['id'], 'stage_index': index}
                    candidates[key].extend(rows)
                stages.append({'page_id': page['id'], 'stage_index': index, 'stage': stage['stage'],
                               'response_id': profile['response_id'], 'response_sha256': response_hash,
                               'diagnostics': diagnostics})
                warnings.extend(review_warnings(diagnostics, page['id']))
        output = args.output / 'recognition' / doc['name']
        output.mkdir(parents=True)
        # Preserve historical protocol/outcome facts, without claiming to rerun them.
        replay = {key: result[key] for key in ('page_coverage', 'completion_status', 'unknown_attempts') if key in result}
        replay.update(candidates=candidates, replay_stages=stages, replay_skipped=skipped,
                      replay_only=True, new_external_calls=0, publication_performed=False)
        replay['parser_review_warnings'] = warnings
        replay['field_extraction_status'] = {key: {
            'valid_values_unique': len({c['value'] for c in rows}) == 1,
            'valid_candidate_count': len(rows),
            'source_parse_status': 'needs_read' if any(w['field'] == key for w in warnings) else 'no_parse_rejection',
            'business_material_complete': 'not_assessed', 'publication_status': 'requires_human_confirmation'}
            for key, rows in candidates.items()}
        apply_adjudication(replay)
        (output / 'result.json').write_text(json.dumps(replay, ensure_ascii=False, indent=2))
        outcome = args.source / 'recognition' / doc['name'] / 'outcome.json'
        if outcome.exists():
            (output / 'outcome.json').write_bytes(outcome.read_bytes())
        counts[doc['name']] = {'responses': len(stages), 'skipped_noncompleted': len(skipped)}
    manifest = {'code_root': str(args.code_root.resolve()), 'source': str(args.source.resolve()),
                'documents': args.documents, 'input_sha256': hashes, 'counts': counts,
                'network_disabled': True, 'new_external_calls': 0}
    (args.output / 'replay_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == '__main__':
    main()
