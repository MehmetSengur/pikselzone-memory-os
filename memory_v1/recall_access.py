"""One source gate shared by every recall surface; diagnostics never expose bodies."""
from __future__ import annotations
import json
import re
from pathlib import Path
from .core import PolicyError, secure_read_text
from .memory_policy import access_reason, config_policy


def source_meta(text: str, relative: str) -> dict:
    meta = {}
    if text.startswith('---\n'):
        # The header ends at the first line that is exactly '---', as in
        # events.parse_event_artifact. Splitting on the first '---' anywhere
        # stopped inside critical_records, whose JSON quotes tool output, before
        # memory_scope was reached: the scope read as empty and a project- or
        # owner-private event passed the gate as unscoped.
        lines = text.splitlines()
        end = lines.index('---', 1) if '---' in lines[1:] else len(lines)
        for line in lines[1:end]:
            if ':' not in line:
                continue
            k, v = line.split(':', 1)
            if k.strip() not in ('memory_scope', 'owner', 'project', 'visibility'):
                continue
            try:
                meta[k.strip()] = json.loads(v.strip())
            except ValueError:
                meta[k.strip()] = v.strip().strip("\"'")
    scope = meta.pop('memory_scope', None)
    if scope is not None:
        if not isinstance(scope, dict):
            raise PolicyError('source-scope-invalid')
        meta.update(scope)
    if relative.startswith(('continuity/', 'threads/')):
        meta.setdefault('project', Path(relative).stem)
    return meta


def source_reason(config, relative: str, *, text: str | None = None, project=None) -> str | None:
    p = config_policy(config, project=project)
    if text is None:
        text, _ = secure_read_text(config.vault_path / relative, root=config.vault_path, max_bytes=2*1024*1024)
    meta = source_meta(text, relative)
    if p.get('owner') and Path(relative).name in ('Last-Session.md', 'Threads.md', 'Journal.md') and not meta.get('owner'):
        return 'scope-unresolved'
    reason = access_reason(meta, p)
    if reason:
        return reason
    # Legacy compiler concepts may cite project-private daily events. Provenance
    # cannot launder those events into shared knowledge; missing sources are partial.
    if relative.startswith('knowledge/'):
        refs = set(re.findall(r'daily/20[0-9-]+/[A-Za-z0-9_.-]+\.md', text))
        for ref in sorted(refs)[:128]:
            try:
                source, _ = secure_read_text(config.vault_path / ref, root=config.vault_path, max_bytes=2*1024*1024)
            except (OSError, PolicyError):
                return 'invalid-source'
            denied = access_reason(source_meta(source, ref), p)
            if denied:
                return denied
        if len(refs) > 128:
            return 'scope-unresolved'
    return None


def filter_items(config, items, *, project=None):
    allowed, rejected = [], []
    for item in items:
        try:
            reason = source_reason(config, item.source_file, project=project)
            if item.item_type == 'knowledge_index':
                from .recall import _concept_slug_from_index_article
                slug = _concept_slug_from_index_article(item.content.split(':', 1)[0])
                if slug and (config.vault_path / 'knowledge' / 'concepts' / (slug + '.md')).is_file():
                    reason = reason or source_reason(config, 'knowledge/concepts/' + slug + '.md', project=project)
        except Exception:
            reason = 'invalid-source'
        if reason:
            rejected.append({'id': item.item_id, 'source': item.source_file, 'reason': reason})
        else:
            allowed.append(item)
    return allowed, rejected
