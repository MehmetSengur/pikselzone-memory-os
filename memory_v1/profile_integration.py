"""Central native Hermes discovery; no per-profile code/config/credential copies.

The operator-owned policy grants access. A profile may narrow capture/consumption,
never expand its grants. Discovery runs in Hermes's active home scope; no history scan.
"""
from __future__ import annotations
import functools
import json
import os
import stat
from pathlib import Path
from typing import Any
from .core import PolicyError, atomic_json, atomic_write, exclusive_lock, iso_now, path_within, secure_read_text, sha256_bytes
from .hermes_guards import is_service_profile_home

VERSION = '1.1.0'
PLUGIN_ID = 'pz-memory-v1'
POLICY_ENV = 'PZ_MEMORY_PROFILE_POLICY'


def load_policy(path: Path | None = None) -> dict:
    value = str(path or os.environ.get(POLICY_ENV, ''))
    if not value or not Path(value).is_absolute():
        raise PolicyError('profile-policy-required')
    p = Path(value)
    text, _ = secure_read_text(p, root=p.parent, max_bytes=128 * 1024)
    info = p.stat()
    if info.st_uid not in (0, os.geteuid()) or stat.S_IMODE(info.st_mode) & 0o022:
        raise PolicyError('profile-policy-not-operator-owned')
    policy = json.loads(text)
    if not isinstance(policy, dict):
        raise PolicyError('profile-policy-schema-invalid')
    if policy.get('schema') != 'pz-memory-profiles-v1':
        raise PolicyError('profile-policy-schema-invalid')
    for key in ('plugin_path', 'base_dir', 'profile_root', 'config_path'):
        if not isinstance(policy.get(key), str) or not Path(policy[key]).is_absolute():
            raise PolicyError('profile-policy-path-invalid:' + key)
    from .memory_policy import resolve_policy
    resolve_policy({'mode':policy.get('mode', 'normal')})
    grants = policy.get('grants', {})
    if not isinstance(grants, dict):
        raise PolicyError('profile-policy-grants-invalid')
    for home, grant in grants.items():
        if not isinstance(home, str) or not Path(home).is_absolute() or not isinstance(grant, dict):
            raise PolicyError('profile-policy-grants-invalid')
        resolve_policy(grant)
        if grant.get('visibility', 'private') not in ('private', 'project', 'shared'):
            raise PolicyError('profile-policy-visibility-invalid')
    return policy


def profile_settings(home: Path, config: dict, policy: dict) -> dict:
    home = home.resolve()
    owner = sha256_bytes(str(home / 'state.db').encode())
    grants = policy.get('grants', {}).get(str(home), {})
    preferences = config.get('pz_memory', {}) or {}
    plugins = config.get('plugins', {}) or {}
    mode = preferences.get('mode', policy.get('mode', 'normal'))
    if mode not in ('normal', 'economic', 'manual'):
        mode = 'manual'  # fail cheap; retain raw evidence
    disabled = plugins.get('disabled', []) or []
    entry = (plugins.get('entries', {}) or {}).get(PLUGIN_ID, {}) or {}
    status, reason = 'configured', 'central-native-discovery'
    if not path_within(home, Path(policy['profile_root'])):
        status, reason = 'out-of-scope', 'profile-outside-trusted-root'
    elif is_service_profile_home(home):
        status, reason = 'service', 'service-role-excludes-chat-capture'
    elif preferences.get('no_memory') is True or PLUGIN_ID in disabled or entry.get('enabled') is False:
        status, reason = 'opt-out', 'explicit-no-memory-or-plugin-disabled'
    return {'schema': 'pz-memory-profile-status-v1', 'version': VERSION,
            'owner': owner, 'profile_home': str(home), 'status': status, 'reason': reason,
            'mode': mode, 'project': grants.get('project', 'unscoped'),
            'visibility': grants.get('visibility', 'private'),
            'projects': list(grants.get('projects', [])), 'shared': grants.get('shared', True),
            'base_dir': policy['base_dir'], 'config_path': policy['config_path']}


def active_settings() -> dict | None:
    if not os.environ.get(POLICY_ENV):
        return None  # legacy path remains available until reviewed activation
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config_readonly as load_config
    return profile_settings(Path(get_hermes_home()), load_config(), load_policy())


def record_status(settings: dict, *, evidence: dict | None = None) -> None:
    p = Path(settings['base_dir']) / 'state' / 'profiles' / (settings['owner'] + '.json')
    with exclusive_lock(p.with_suffix('.lock')):
        previous = {}
        if p.exists():
            try:
                previous = json.loads(secure_read_text(p, root=p.parent, max_bytes=64 * 1024)[0])
            except (ValueError, OSError, PolicyError):
                pass
        data = {**previous, **settings, 'checked_at': iso_now()}
        # Only the actual plugin callback supplies evidence; discovery never means verified.
        if evidence:
            kind = evidence['kind']
            data['last_' + kind] = {k: v for k, v in evidence.items() if k != 'kind'}
        atomic_write(p, (json.dumps(data, ensure_ascii=False, sort_keys=True) + '\n').encode(), mode=0o640)


def install_discovery(plugins: Any, *, policy: dict, get_home=None, load_config=None) -> None:
    if getattr(plugins, '_pz_memory_central_discovery', False):
        return
    if get_home is None:
        from hermes_constants import get_hermes_home as get_home
    if load_config is None:
        from hermes_cli.config import load_config_readonly as load_config
    original_collect = plugins.collect_directory_manifests
    original_enabled = plugins._get_enabled_plugins
    original_disabled = plugins._get_disabled_plugins

    def current():
        return profile_settings(Path(get_home()), load_config(), policy)

    @functools.wraps(original_collect)
    def collect():
        settings = current()
        manifests = [m for m in original_collect() if m.name != PLUGIN_ID and getattr(m, 'key', '') != PLUGIN_ID]
        if settings['status'] == 'configured':
            source = Path(policy['plugin_path'])
            manifest = plugins.parse_manifest_file(source / 'plugin.yaml', source, 'user', '')
            if manifest is not None:
                manifests.append(manifest)
            else:
                settings.update(status='degraded', reason='central-manifest-invalid')
        record_status(settings)
        return manifests

    def enabled():
        values = set(original_enabled() or ())
        if current()['status'] == 'configured':
            values.add(PLUGIN_ID)
        else:
            values.discard(PLUGIN_ID)
        return values

    def disabled():
        values = set(original_disabled() or ())
        if current()['status'] != 'configured':
            values.add(PLUGIN_ID)
        return values

    plugins.collect_directory_manifests = collect
    plugins._get_enabled_plugins = enabled
    plugins._get_disabled_plugins = disabled
    plugins._pz_memory_central_discovery = True


def install_native() -> bool:
    if not os.environ.get(POLICY_ENV):
        return False
    from hermes_cli import plugins, config as native_config
    policy = load_policy()
    def with_defaults(original):
        @functools.wraps(original)
        def load_config(*args, **kwargs):
            import copy
            cfg = copy.deepcopy(original(*args, **kwargs))
            entry = cfg.setdefault('plugins', {}).setdefault('entries', {}).setdefault(PLUGIN_ID, {})
            for key, value in policy.get('plugin_defaults', {}).items():
                entry.setdefault(key, copy.deepcopy(value))
            return cfg
        load_config._pz_memory_defaults = True
        return load_config
    # PluginLlm intentionally reads the readonly facade. Both facades receive
    # the same in-memory defaults; neither writes per-profile config files.
    for name in ('load_config', 'load_config_readonly'):
        original = getattr(native_config, name)
        if not getattr(original, '_pz_memory_defaults', False):
            setattr(native_config, name, with_defaults(original))
    install_discovery(plugins, policy=policy)
    return True


def install_desktop_lifecycle(server=None) -> bool:
    """Restore the native session's home on background teardown threads.

    The gateway already stores profile_home on each live session. Bind that
    exact identity around the native finalizer, including hook discovery and
    provider configuration; never infer ownership by scanning session IDs.
    """
    if not os.environ.get(POLICY_ENV):
        return False
    if server is None:
        from tui_gateway import server
    original = server._finalize_session
    if getattr(original, '_pz_memory_profile_scope', False) is True:
        return True
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    @functools.wraps(original)
    def finalize(session, *args, **kwargs):
        if not session:
            return original(session, *args, **kwargs)
        home = session.get('profile_home') or server._hermes_home
        token = set_hermes_home_override(str(home))
        try:
            return original(session, *args, **kwargs)
        finally:
            reset_hermes_home_override(token)

    finalize._pz_memory_profile_scope = True
    server._finalize_session = finalize
    return True


def profile_report(base: Path) -> list[dict]:
    rows = []
    for p in sorted((base / 'state' / 'profiles').glob('*.json'))[:256]:
        try:
            s = json.loads(secure_read_text(p, root=p.parent, max_bytes=64 * 1024)[0])
            rows.append({k: s.get(k) for k in ('owner', 'profile_home', 'version', 'status', 'reason',
                                             'project', 'projects', 'mode', 'last_capture', 'last_recall')})
        except (ValueError, OSError, PolicyError):
            rows.append({'owner': p.stem, 'status': 'degraded', 'reason': 'profile-status-invalid'})
    return rows


def settings_for_home(home: Path) -> dict | None:
    if not os.environ.get(POLICY_ENV):
        return None
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config_readonly as load_config
    token = set_hermes_home_override(str(home))
    try:
        return profile_settings(home, load_config(), load_policy())
    finally:
        reset_hermes_home_override(token)


def profile_recall(settings: dict, *, session_id: str, query: str, first: bool, receipt_factory):
    import dataclasses
    from .core import MemoryConfig
    from .recall import build_startup_recall_bundle, targeted_recall
    if settings['mode'] == 'manual':
        return None
    config = MemoryConfig.load(Path(settings['config_path']))
    config = dataclasses.replace(config, memory={
        **config.memory, 'owner': settings['owner'], 'project': settings['project'],
        'projects': settings['projects'], 'shared': settings['shared'], 'mode': settings['mode']})
    if first:
        bundle = build_startup_recall_bundle(config, runtime='hermes', session_key=session_id,
            continuity_scope=settings['project'] if settings['project'] != 'unscoped' else None,
            budget_chars=config.context_budget_chars - 4002 if query.strip() and config.context_budget_chars >= 6000 else config.context_budget_chars)
        text, audit, sources, digest = bundle.text, bundle.selection_audit, bundle.source_shas, bundle.bundle_sha256
        if query.strip() and config.context_budget_chars >= 6000:
            targeted = targeted_recall(config, query, budget_chars=4000, max_items=3)
            if targeted['results']:
                text += '\n' + targeted['markdown']
                sources = {**sources, **{r['source']:r['sha256'] for r in targeted['results']}}
                digest = sha256_bytes(text.encode())
            audit = {'startup':audit, 'targeted':targeted['selection_audit']}

    else:
        if not query.strip():
            return None
        result = targeted_recall(config, query, budget_chars=4000, max_items=3)
        if not result['results']:
            return None
        text, audit, digest = result['markdown'], result['selection_audit'], result['digest']
        sources = {r['source']: r['sha256'] for r in result['results']}
    receipt_dir = Path(settings['base_dir']) / 'state' / 'receipts' / 'profiles' / settings['owner']
    receipt = receipt_factory(session_id, 'pre_llm_call', target_dir=str(receipt_dir))
    evidence = {'kind': 'recall', 'session_id':session_id, 'observed_at':iso_now(),
        'native_invoke': bool(receipt and receipt.get('native_invoke')), 'bundle_sha256':digest,
        'delivery':'returned-to-native-pre-llm-hook', 'answer_verified':False,
        'sources': sources, 'selection_audit': _bounded_audit(audit)}
    record_status(settings, evidence=evidence)
    return {'context': text}


def reconcile_profiles(policy: dict | None = None) -> list[dict]:
    """Operator-invoked bounded inventory. No config, SOUL or credential writes."""
    policy = policy or load_policy()
    root = Path(policy['profile_root'])
    homes = [root]
    profiles = root / 'profiles'
    if profiles.is_dir():
        homes.extend(sorted(p for p in profiles.iterdir() if p.is_dir() and not p.is_symlink())[:255])
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from hermes_cli.config import load_config_readonly as load_config
    for home in homes:
        token = set_hermes_home_override(str(home))
        try:
            record_status(profile_settings(home, load_config(), policy))
        finally:
            reset_hermes_home_override(token)
    return profile_report(Path(policy['base_dir']))


def _bounded_audit(value):
    """Status is a bounded diagnostic, never an unbounded copy of all candidates."""
    if isinstance(value, list):
        return {'count':len(value), 'sample':[_bounded_audit(v) for v in value[:32]],
                'truncated':len(value)>32}
    if isinstance(value, dict):
        return {k:_bounded_audit(v) for k,v in value.items()}
    return value
