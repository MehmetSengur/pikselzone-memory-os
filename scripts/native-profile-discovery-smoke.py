#!/usr/bin/env python3
"""Isolated real-Hermes discovery compatibility, NOT a conversation PASS.

Run with the installed Hermes Python and candidate memory-os on PYTHONPATH.
Creates only a fresh directory under --scratch. No hooks are manually invoked,
no LLM calls, production config edits, credential copies or service restarts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

parser=argparse.ArgumentParser()
parser.add_argument('--scratch',required=True,type=Path)
parser.add_argument('--output',required=True,type=Path)
args=parser.parse_args()
root=args.scratch.resolve()/('native-discovery-'+uuid.uuid4().hex[:12])
root.mkdir(parents=True)
home=root/'hermes';home.mkdir()
os.environ['HERMES_HOME']=str(home)
os.environ.pop('PZ_MEMORY_TEST_MODE',None)
source=Path(__file__).resolve().parents[1]
policy={'schema':'pz-memory-profiles-v1','plugin_path':str(source/'hermes_plugins/pz-memory-v1'),
        'base_dir':str(root/'memory'),'profile_root':str(home),'config_path':str(root/'memory-config.json'),
        'plugin_defaults':{'llm':{'allow_model_override':True,'allowed_models':['gpt-5.6-luna']}},'grants':{}}
policy_path=root/'policy.json';policy_path.write_text(json.dumps(policy));policy_path.chmod(0o600)
os.environ['PZ_MEMORY_PROFILE_POLICY']=str(policy_path)
from hermes_cli import profiles, plugins
from hermes_constants import set_hermes_home_override,reset_hermes_home_override
from memory_v1.profile_integration import install_native, reconcile_profiles
from agent.plugin_llm import _resolve_trust_policy

def fingerprint(p):
    return {name:hashlib.sha256((p/name).read_bytes()).hexdigest() if (p/name).exists() else None
            for name in ('config.yaml','SOUL.md','.env','auth.json')}

def create():
    return profiles.create_profile('review-'+uuid.uuid4().hex[:8],no_alias=True,no_skills=True)

existing=create();baseline=fingerprint(existing)
install_native();install_native()
new=create();new_baseline=fingerprint(new)
checks={}
for label,profile in [('existing',existing),('new',new)]:
    token=set_hermes_home_override(profile)
    try:
        manager=plugins.PluginManager();manager.discover_and_load();manager.discover_and_load()
        loaded=manager._plugins.get('pz-memory-v1')
        assert loaded and loaded.enabled and not loaded.error, (label, str(loaded))
        assert Path(loaded.manifest.path)==source/'hermes_plugins/pz-memory-v1'
        assert all(len(callbacks)==1 for callbacks in manager._hooks.values())
        trust=_resolve_trust_policy('pz-memory-v1')
        assert trust.allow_model_override and 'gpt-5.6-luna' in trust.allowed_models
        assert not (profile/'plugins/pz-memory-v1').exists()
        assert not (profile/'auth.json').exists()
        assert (profile/'.env').read_text()==profiles._PLACEHOLDER_ENV
        checks[label]={'profile':str(profile),'central_manifest':loaded.manifest.path,
                       'hook_counts':{k:len(v) for k,v in manager._hooks.items()},
                       'readonly_llm_trust':True,'native_placeholder_env_only':True}
    finally:
        reset_hermes_home_override(token)
assert fingerprint(existing)==baseline and fingerprint(new)==new_baseline
preserved={}
for name in ('checkpoints/isolated-marker.json','finalize-retry/isolated-marker.json'):
    path=root/'memory/state'/name;path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('{"scope":"isolated-retention-fixture-not-lifecycle-evidence"}\n')
    preserved[path]=path.read_bytes()
reconcile_profiles(policy);reconcile_profiles(policy)
assert all(path.read_bytes()==body for path,body in preserved.items())
assert fingerprint(existing)==baseline and fingerprint(new)==new_baseline

# Explicit opt-out and a genuine service marker remain effective after reload.
for label,profile in [('opt-out',create()),('service',create())]:
    if label=='opt-out':
        (profile/'config.yaml').write_text('pz_memory:\n  no_memory: true\n')
    else:
        (profile/'service-profile.json').write_text(json.dumps({'schema':'pikselzone-service-profile-v1','service':'compiler'}))
    token=set_hermes_home_override(profile)
    try:
        manager=plugins.PluginManager();manager.discover_and_load();manager.discover_and_load(force=True)
        assert 'pz-memory-v1' not in manager._plugins or not manager._plugins['pz-memory-v1'].enabled
        # Memory opt-out does not revoke the existing compiler's plugin LLM trust.
        assert _resolve_trust_policy('pz-memory-v1').allow_model_override
        checks[label]={'capture_disabled':True,'compiler_trust_retained':True}
    finally:
        reset_hermes_home_override(token)

# A fresh old-entry process does not install the central shim. Rollback does not
# modify config, raw checkpoints or auth; old per-profile sources remain on disk.
rollback_env=dict(os.environ,HERMES_HOME=str(new))
result=subprocess.run([sys.executable,'-c',"from hermes_cli.plugins import PluginManager; p=PluginManager(); p.discover_and_load(); assert 'pz-memory-v1' not in p._plugins"],env=rollback_env,capture_output=True,text=True,timeout=60)
assert result.returncode==0, 'fresh-process-rollback-failed'
assert fingerprint(new)==new_baseline
assert all(path.read_bytes()==body for path,body in preserved.items())
report={'schema':'pz-native-profile-discovery-review-v1','status':'pass',
        'scope':'isolated-native-profile-creation-and-discovery-only',
        'checks':checks,'repeat_reconciliation_preserved_settings':True,'raw_and_retry_retention_fixture_unchanged':True,'fresh_process_rollback':True,
        'source_sha256':hashlib.sha256((source/'memory_v1/profile_integration.py').read_bytes()).hexdigest(),
        'plugin_sha256':hashlib.sha256((source/'hermes_plugins/pz-memory-v1/__init__.py').read_bytes()).hexdigest(),
        'conversation_capture_publish_recall_verified':False,'production_modified':False,
        'native_receipts_manually_created':False}
args.output.write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report))
