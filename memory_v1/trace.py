"""Read-only bounded trace. Content is never copied into diagnostic output."""
from __future__ import annotations
import json
from pathlib import Path
from .core import secure_read_text, sha256_bytes
from .events import parse_event_artifact
from .memory_policy import config_policy, access_reason
from .recall import targeted_recall, _tokenize
from .recall_access import source_reason

LIMIT = 512


def _json(path):
    return json.loads(secure_read_text(path, root=path.parent, max_bytes=2*1024*1024)[0])


def trace_memory(config, query: str, *, session_id: str = '') -> dict:
    recall = targeted_recall(config, query, max_items=10)
    terms, policy = _tokenize(query), config_policy(config)
    bases = [root/'memory-v1' for root in config.transcript_roots.get('hermes', ())]
    # Operator-configured central path takes priority; never invent a Docker root.
    import os
    if os.environ.get('PZ_MEMORY_PROFILE_POLICY'):
        from .profile_integration import load_policy
        bases = [Path(load_policy()['base_dir'])]
    issues, checkpoints, retries, traces = [], [], [], []
    limited = False
    roots = [config.state_path/'queue'/'pending'] + [b/'state'/'checkpoints' for b in bases]
    retry_roots = [config.state_path/'queue'/'retry'] + [b/'state'/'finalize-retry' for b in bases]
    for roots_group, output in ((roots, checkpoints), (retry_roots, retries)):
        for root in roots_group:
            paths = sorted(root.glob('*.json'))
            limited |= len(paths) > LIMIT
            for path in paths[:LIMIT]:
                try:
                    value = _json(path)
                    if session_id and value.get('session_id') != session_id:
                        continue
                    if value.get('database'):
                        value['owner'] = sha256_bytes(value['database'].encode())
                    if access_reason(value, policy):
                        continue
                    value['_reference'] = path.name
                    output.append(value)
                except Exception:
                    issues.append({'source':sha256_bytes(str(path).encode()), 'reason':'invalid-source'})
    pending = []
    for cp in checkpoints:
        text = cp.get('normalized_transcript', '')
        if not isinstance(text, str) or not (terms & _tokenize(text)):
            continue
        digest = cp.get('source_digest', cp.get('turn_digest'))
        reason = 'not-yet-processed'
        if sha256_bytes(text.encode()) != digest:
            reason = 'source-changed'
        retry = next((r for r in retries if r.get('session_id') == cp.get('session_id') and r.get('source_sha256', r.get('source_digest')) == digest), None)
        if retry and reason != 'source-changed':
            reason = 'retry-waiting' if retry.get('status') == 'retry-scheduled' else retry.get('status', 'retry-recorded')
        pending.append({'checkpoint':cp['_reference'], 'reason':reason, 'source_sha256':digest,
                        'retry_reason': retry.get('reason_code') if retry else None})

    event_paths = [(p, 'daily') for p in sorted((config.vault_path/'daily').glob('20*/*.md'), reverse=True)]
    for base in bases:
        event_paths.extend((p, 'outbox') for p in sorted((base/'outbox'/'events').rglob('*.md')))
    limited |= len(event_paths) > LIMIT
    for path, location in event_paths[:LIMIT]:
        reference = str(path.relative_to(config.vault_path)) if location == 'daily' else 'outbox/events/' + path.name
        try:
            text, digest = secure_read_text(path, root=config.vault_path if location == 'daily' else path.parent, max_bytes=2*1024*1024)
            event = parse_event_artifact(text)
            if session_id and event['session_id'] != session_id:
                continue
            scope = event.get('memory_scope', {'project':event.get('project', 'unscoped')})
            reason = access_reason(scope, policy)
            if reason:
                # Do not enumerate private claim IDs or content even in diagnostics.
                continue
            records = event.get('critical_records', [])
            matches = [r for r in records if terms & _tokenize(r['text'])]
            summary = ' '.join(sum(event['sections'].values(), []))
            if not matches and not terms & _tokenize(summary):
                continue
            superseded = {v for r in records for v in r['supersedes']}
            selected = [r for r in recall['selection_audit'] if r.get('source') == reference]
            reason = next((r['reason'] for r in selected if r['reason']=='selected-for-context'), selected[0]['reason'] if selected else 'no-match-or-scan-limit')
            cp = next((c for c in checkpoints if c.get('session_id') == event['session_id'] and c.get('source_digest',c.get('turn_digest')) == event['source_sha256']), None)
            knowledge_refs = []
            if location == 'daily':
                concepts = sorted((config.vault_path/'knowledge').glob('*/*.md'))
                limited |= len(concepts) > LIMIT
                for concept in concepts[:LIMIT]:
                    relative = str(concept.relative_to(config.vault_path))
                    try:
                        body, sha = secure_read_text(concept, root=config.vault_path, max_bytes=2*1024*1024)
                        if reference in body and not source_reason(config, relative, text=body):
                            knowledge_refs.append({'source':relative, 'sha256':sha})
                    except Exception:
                        issues.append({'source':relative, 'reason':'invalid-source'})
            delivered = None
            if scope.get('owner'):
                for base in bases:
                    try:
                        evidence = _json(base/'state'/'profiles'/(scope['owner']+'.json')).get('last_recall',{})
                        if (evidence.get('native_invoke') and evidence.get('sources',{}).get(reference) == digest):
                            delivered = {'session_id':evidence.get('session_id'), 'bundle_sha256':evidence.get('bundle_sha256')}
                    except Exception:
                        pass
            traces.append({'source':reference, 'sha256':digest, 'source_sha256':event['source_sha256'],
                'session_id':event['session_id'], 'owner':scope.get('owner',''), 'stages':[
                    {'stage':'raw-source','state':'referenced','reason':'normalized-source-hash-retained-not-reread'},
                    {'stage':'checkpoint','state':'present' if cp else 'not-observed','reason':'pending' if cp else 'may-have-settled'},
                    {'stage':'structured-record','state':'present' if matches else 'absent','reason':'source-linked' if matches else 'not-extracted-or-legacy',
                     'records':[{'id':r['id'],'reason':'superseded' if r['id'] in superseded else 'conflict-unresolved' if r['conflicts_with'] else 'recorded-claim'} for r in matches]},
                    {'stage':'summary','state':'present','reason':'summary-loss' if matches and any(r['text'] not in summary for r in matches) else 'summary-available'},
                    {'stage':'outbox','state':'present' if location=='outbox' else 'not-observed','reason':'awaiting-publisher' if location=='outbox' else 'may-have-published'},
                    {'stage':'daily','state':'present' if location=='daily' else 'not-observed','reason':'published' if location=='daily' else 'not-yet-published'},
                    {'stage':'knowledge','state':'present' if knowledge_refs else 'not-observed','reason':'source-reference' if knowledge_refs else 'not-processed-or-not-generalizable','references':knowledge_refs},
                    {'stage':'recall','state':'candidate' if selected else 'absent','reason':reason},
                    {'stage':'model-context','state':'hook-return-observed' if delivered else 'unverified','reason':'native-hook-return-not-answer-proof' if delivered else 'prepared-is-not-delivered','evidence':delivered},
                    {'stage':'answer','state':'unverified','reason':'no-answer-evidence'}]})
        except Exception as exc:
            reason = 'source-changed' if 'source-changed' in str(exc) else 'invalid-source'
            issues.append({'source':reference, 'reason':reason})
    # Scope exclusions are safe references, not the excluded text.
    for row in recall['selection_audit']:
        if row['reason'] in ('scope-excluded','scope-unresolved','invalid-source'):
            issues.append({'source':sha256_bytes(row.get('source','').encode()),'reason':row['reason']})
    return {'schema':'pikselzone-memory-trace-v1','status':'partial' if issues or limited else 'ok',
            'query_sha256':sha256_bytes(query.encode()),'traces':traces,'pending':pending,'issues':issues,
            'scan_limit':LIMIT,'scan_limit_reached':limited,'answer_verified':False}
