"""Shared consumption and access policy. Text/LLM output cannot grant access."""
from __future__ import annotations
from .core import ConfigError

MODES = ('normal', 'economic', 'manual')


def resolve_policy(base: dict | None = None, profile: dict | None = None, session: dict | None = None) -> dict:
    base = dict(base or {})
    p = {'mode': 'normal', 'no_memory': False, 'owner': '', 'project': 'unscoped',
         'projects': [], 'shared': True, **base}
    for override in (profile, session):
        if override:
            p['mode'] = override.get('mode', p['mode'])
            p['no_memory'] = p['no_memory'] or override.get('no_memory') is True
    if (p['mode'] not in MODES or not isinstance(p['projects'], list)
        or any(not isinstance(x, str) for x in p['projects'])
        or not isinstance(p['owner'], str) or not isinstance(p['project'], str)
        or not isinstance(p['shared'], bool) or not isinstance(p['no_memory'], bool)):
        raise ConfigError('memory-policy-invalid')
    off = p['no_memory'] is True
    automatic = not off and p['mode'] != 'manual'
    p.update(capture=not off, summarize=automatic, compile=automatic, recall=automatic,
             budget_scale=0.5 if p['mode'] == 'economic' else 1.0,
             processing_interval=900 if p['mode'] == 'economic' else 0)
    return p


def access_reason(meta: dict, policy: dict) -> str | None:
    """Same gate for startup, targeted, associative, late and delegated context."""
    if meta.get('visibility') == 'shared':
        return None if policy.get('shared', True) else 'scope-excluded'
    owner = meta.get('owner')
    project = meta.get('project', 'unscoped')
    project_access = project not in (None, '', 'unscoped') and project in {
        policy.get('project'), *policy.get('projects', [])}
    if owner and owner != policy.get('owner'):
        if meta.get('visibility') != 'project' or not project_access:
            return 'scope-excluded'
    if project not in (None, '', 'unscoped') and not project_access:
        return 'scope-excluded'
    if meta.get('visibility') == 'private' and not owner and project in (None, '', 'unscoped'):
        return 'scope-unresolved'
    return None


def config_policy(config, *, project=None):
    data = dict(config.memory)
    if project:
        data['project'] = project
    return resolve_policy(data, data.get('profile'), data.get('session'))
