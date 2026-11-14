#!/usr/bin/env python3
"""Release verification; emits counts and paths, never secret values."""
import hashlib
import json
from pathlib import Path
import subprocess

ROOT=Path(__file__).resolve().parent
baseline=json.loads((ROOT/'baseline.json').read_text())
changed=[name for name,digest in baseline.items() if hashlib.sha256((ROOT.parent/name).read_bytes()).hexdigest()!=digest]
assert not changed,'Preexisting user files changed: '+str(changed)
token=json.loads((Path.home()/'.config/bili-helper-codex/config.json').read_text())['token'].encode()
checked=[]
for path in ROOT.rglob('*'):
    if not path.is_file() or any(part in ('upstream','toolchain','build','backups','__pycache__') for part in path.relative_to(ROOT).parts): continue
    if token in path.read_bytes(): raise AssertionError('Secret detected in artifact: '+str(path))
    checked.append(str(path.relative_to(ROOT)))
installed=Path.home()/'.local/opt/wiliwili-ai'
for name in ['service.py','launch.py']:
    assert (installed/name).read_bytes()==(ROOT/name).read_bytes(),'Installed source differs: '+name
assert (installed/'bin/wiliwili').read_bytes()==(ROOT/'build/wiliwili').read_bytes(),'Installed binary differs'
deps=subprocess.run(['ldd',str(installed/'bin/wiliwili')],capture_output=True,text=True,check=True).stdout
assert 'not found' not in deps,'Missing runtime dependency'
assert (installed/'resources/icon/icon.png').is_file(),'Missing application icon'
result=dict(preexisting_files_checked=len(baseline),preexisting_files_unchanged=True,
            secret_scan_files=len(checked),secret_found=False,installed_matches_build=True,
            all_runtime_dependencies_resolved=True,
            binary_sha256=hashlib.sha256((installed/'bin/wiliwili').read_bytes()).hexdigest())
(ROOT/'validation/audit-results.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
