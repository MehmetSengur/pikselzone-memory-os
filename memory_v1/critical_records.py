"""Bounded source-linked claims preserved beside summaries, never native receipts.

No model call. Only authored user spans are automatically retained; assistant
assertions do not establish tool verification. Exact redacted spans are hashed.
"""
from __future__ import annotations
import re
import json
from types import SimpleNamespace
from .core import SchemaError, redact_sensitive_text, sha256_bytes
from . import provenance as pv

SCHEMA = 'pikselzone-critical-record-v1'
MAX_RECORDS = 128
MAX_TEXT = 4096
CRITICAL = re.compile(r'\d|\b(karar|düzeltme|correction|decision)\s*:|\b(decided|kararlaştırdık|kalıcı tercihim)\b', re.I)
TEST = re.compile(r'\b(canary|test|fixture|kontrol değeri|test değeri)\b', re.I)
CORRECTION = re.compile(r'\b(düzeltme|düzeltiyorum|yerine|correction|instead of)\b', re.I)


def _subject(text):
    sku = re.search(r'\b(SKU|barkod|barcode)\s*[:#]?\s*([\w-]+)', text, re.I)
    if sku:
        return sku.group(1).lower() + ':' + sku.group(2).lower()
    value = re.sub(r'^(düzeltme|karar|correction|decision)\s*:\s*', '', text, flags=re.I)
    if ':' in value:
        return value.split(':', 1)[0].strip().lower()[:120]
    return ' '.join(re.findall(r'[^\W\d_]+', value.lower())[:3])


def extract_records(text: str, *, runtime: str, session_id: str, owner: str = '',
                    project: str = 'unscoped', source_ref: str = '') -> list[dict]:
    text, _ = redact_sensitive_text(text)
    digest = sha256_bytes(text.encode())
    chunks = re.split(r'(?m)^(USER|ASSISTANT|TOOL(?:\[[A-Fa-f0-9]+\])?):\s*', text)
    records = []
    previous_assistant = ''
    for i in range(1, len(chunks), 2):
        role, body = chunks[i:i+2]
        if role == 'ASSISTANT':
            previous_assistant = body
            continue
        tool_id = bytes.fromhex(role[5:-1]).decode() if role.startswith('TOOL[') else ''
        if (role != 'USER' and not tool_id) or TEST.search(body):
            continue
        turn = (SimpleNamespace(is_task_prompt=False, blocks=[SimpleNamespace(provenance=pv.AUTHORED, text=body)])
                if tool_id else pv.analyze_user_turn(body, prior_assistant_text=previous_assistant))
        # Task briefs, relayed instructions and quoted logs are data, not user decisions.
        if turn.is_task_prompt and re.search(r'(?im)^\s*(görev|task|instructions|amaç)\s*:', body):
            continue
        for block in turn.blocks:
            if block.provenance != pv.AUTHORED:
                continue
            for sentence in re.split(r'(?<=[.!?])\s+|\n+', block.text):
                sentence = sentence.strip()
                if not sentence or not CRITICAL.search(sentence):
                    continue
                if len(sentence) > MAX_TEXT or len(records) >= MAX_RECORDS:
                    raise SchemaError('critical-record-capacity-exceeded-source-retained')
                reference = (source_ref or runtime + ':' + session_id + ':normalized-' + digest) + '#message-' + str((i-1)//2)
                value_sha = sha256_bytes(sentence.encode())
                identity = sha256_bytes((owner + '\0' + reference + '\0' + value_sha).encode())
                subject = _subject(sentence)
                prior = [r['id'] for r in records if r['subject'] == subject]
                correction = bool(CORRECTION.search(sentence))
                records.append({'schema': SCHEMA, 'id': identity, 'text': sentence,
                    'claim': 'tool-result' if tool_id else 'user-statement',
                    **({'tool_call_id':tool_id, 'verification':'native-result-captured-not-independent-truth'} if tool_id else {}), 'source_ref': reference, 'source_sha256': digest,
                    'text_sha256': value_sha, 'runtime': runtime, 'session_id': session_id,
                    'owner': owner, 'project': project, 'subject': subject,
                    'supersedes': prior if correction else [],
                    'conflicts_with': [] if correction else prior,
                    'authority': 'derived-claim-not-operational-truth'})
    return records


def validate_records(records):
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise SchemaError('critical-records-invalid')
    for r in records:
        if not isinstance(r, dict) or r.get('schema') != SCHEMA:
            raise SchemaError('critical-record-schema-invalid')
        for k in ('id', 'source_sha256', 'text_sha256'):
            if not re.fullmatch('[0-9a-f]{64}', str(r.get(k, ''))):
                raise SchemaError('critical-record-hash-invalid')
        if not isinstance(r.get('text'), str) or len(r['text']) > MAX_TEXT:
            raise SchemaError('critical-record-text-invalid')
        if sha256_bytes(r['text'].encode()) != r['text_sha256']:
            raise SchemaError('critical-record-source-changed')
        if r.get('claim') not in ('user-statement', 'tool-result', 'agent-inference'):
            raise SchemaError('critical-record-claim-invalid')
        if r['claim'] == 'tool-result' and not r.get('tool_call_id'):
            raise SchemaError('critical-record-tool-evidence-required')
        expected = sha256_bytes((r.get('owner', '') + '\0' + r.get('source_ref', '') + '\0' + r['text_sha256']).encode())
        if expected != r['id'] or r.get('authority') != 'derived-claim-not-operational-truth':
            raise SchemaError('critical-record-identity-invalid')
        for key in ('supersedes', 'conflicts_with'):
            if not isinstance(r.get(key), list) or any(not re.fullmatch('[0-9a-f]{64}', str(v)) for v in r[key]):
                raise SchemaError('critical-record-relation-invalid')


def merge_records(*groups):
    result = {}
    for group in groups:
        validate_records(group)
        for r in group:
            result[r['id']] = r
    if len(result) > MAX_RECORDS:
        raise SchemaError('critical-record-capacity-exceeded-source-retained')
    return list(result.values())


def render_records(records):
    validate_records(records)
    superseded = {v for r in records for v in r['supersedes']}
    lines = []
    for r in records:
        state = 'superseded' if r['id'] in superseded else ('conflict-unresolved' if r['conflicts_with'] else 'recorded-claim')
        lines.append(f"- [{r['claim']}; {state}; source={r['source_ref']}; id={r['id'][:16]}] {r['text']}")
    return '\n'.join(lines)


def native_tool_results(record: dict) -> list[tuple[str, str]]:
    """Extract only runtime tool-result envelopes, never assistant prose claims.

    The call id is encoded in normalization so pasted text cannot create a role.
    The result is evidence of what a tool reported, not external ground truth.
    """
    msg = record.get('payload') if record.get('type') == 'response_item' else record.get('message', record)
    if not isinstance(msg, dict):
        return []
    results = []
    if msg.get('role') == 'tool' and msg.get('tool_call_id'):
        results.append((str(msg['tool_call_id']), msg.get('content', '')))
    elif msg.get('type') in ('function_call_output', 'custom_tool_call_output') and msg.get('call_id'):
        results.append((str(msg['call_id']), msg.get('output', '')))
    elif isinstance(msg.get('content'), list):
        for block in msg['content']:
            if isinstance(block, dict) and block.get('type') == 'tool_result' and block.get('tool_use_id'):
                results.append((str(block['tool_use_id']), block.get('content', '')))
    output = []
    for call_id, content in results:
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        clean, _ = redact_sensitive_text(content)
        if CRITICAL.search(clean) and len(call_id) <= 256:
            output.append(('tool[' + call_id.encode().hex() + ']', re.sub(r'\s+', ' ', clean).strip()))
    return output
