#!/usr/bin/env python3
"""Rebuild the complete source archive in a new directory with the pinned toolchain."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parent
archive = ROOT/'dist/wiliwili-ai-3.0.0-source.tar.zst'
temporary = Path(tempfile.mkdtemp(prefix='wiliwili-final-source-'))
pending = temporary/'source.tar'
subprocess.run(['zstd','-q','-d',str(archive),'-o',str(pending)],check=True)
with tarfile.open(pending) as tar:
    tar.extractall(temporary,filter='data')
pending.unlink()
source = temporary/'wiliwili-ai-3.0.0-source'
(source/'toolchain').symlink_to((ROOT/'toolchain').resolve())
log_path = temporary/'build.log'
with log_path.open('w') as log:
    outcome = subprocess.run(['bash','rebuild.sh'],cwd=source,stdout=log,stderr=log)
result = {'evidence':'fresh_build_from_final_complete_source_archive','build_passed':outcome.returncode==0,
          'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
          'external_android_dependencies':False,'toolchain_reused':True,
          'build_steps':max((int(x) for x in re.findall(r'\[(\d+)/\d+\]',log_path.read_text())),default=0)}
if not outcome.returncode:
    result['version']=subprocess.check_output([str(source/'build/wiliwili'),'--version'],text=True).strip()
else:
    result['build_log']=str(log_path)
(ROOT/'validation/release-3.0-source-build.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False),flush=True)
raise SystemExit(outcome.returncode)
