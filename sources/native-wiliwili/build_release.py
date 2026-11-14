#!/usr/bin/env python3
"""Create Arch x86_64 runtime and complete GPL source archives from allowlists."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import tarfile
import tempfile
from install import payload, VERSION

ROOT=Path(__file__).resolve().parent


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest()


def archive(source, name, target):
    with tempfile.NamedTemporaryFile(suffix='.tar') as pending:
        with tarfile.open(pending.name,'w') as tar:
            for p in sorted(source.rglob('*')):
                if not p.is_file(): continue
                info=tar.gettarinfo(str(p),arcname=str(Path(name)/p.relative_to(source)))
                info.uid=info.gid=0;info.uname=info.gname='';info.mtime=0
                with p.open('rb') as file:tar.addfile(info,file)
        subprocess.run(['zstd','-q','-f','-T2',pending.name,'-o',str(target)],check=True)


def main():
    version=subprocess.check_output([str(ROOT/'build/wiliwili'),'--version'],text=True).strip()
    revision=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    native_source=[p for p in sorted(ROOT.iterdir()) if p.suffix in ('.cpp','.hpp','.py','.sh','.patch') and not p.name.startswith(('validate_','make_report','online_check','audit'))]
    sourcehash=hashlib.sha256(b''.join(p.name.encode()+p.read_bytes() for p in native_source)).hexdigest()
    info=dict(version=VERSION,upstream_version='1.6.0',upstream_commit='88e5876bea9502d06f46a8656e3530684d3aaf7d',
              checkpoint=revision,uncommitted_sources=True,source_sha256=sourcehash,binary_sha256=digest(ROOT/'build/wiliwili'),
              platform='Arch Linux',architecture=platform.machine(),native=version)
    (ROOT/'build-info.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n')
    dist=ROOT/'dist';dist.mkdir(exist_ok=True);name='wiliwili-ai-'+VERSION+'-arch-x86_64'
    with tempfile.TemporaryDirectory(prefix='wiliwili-release-') as temporary:
        temp=Path(temporary);runtime=temp/'runtime';payload(ROOT,runtime)
        shutil.copy2(ROOT/'install.py',runtime/'install.py')
        licenses=runtime/'licenses';licenses.mkdir(exist_ok=True);upstream=ROOT/'upstream'
        for p in upstream.rglob('*'):
            if p.is_file() and not p.is_symlink() and '.git' not in p.parts and p.name.lower() in ('license','license.txt','license.md','copying'):
                target=licenses/p.relative_to(upstream);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,target)
        archive(runtime,name,dist/(name+'.tar.zst'))
        source=temp/'source';source.mkdir()
        for p in native_source:shutil.copy2(p,source/p.name)
        bridge=ROOT/'bridge.py' if (ROOT/'bridge.py').exists() else ROOT.parent/'local-codex-bridge/server.py'
        shutil.copy2(bridge,source/'bridge.py')
        for name2 in ['README.md','CHANGELOG.md','LICENSE','build-info.json','.gitignore']:
            shutil.copy2(ROOT/name2,source/name2)
        shutil.copytree(upstream,source/'upstream',ignore=shutil.ignore_patterns('.git','__pycache__','build','cmake-build-*'),symlinks=True)
        archive(source,'wiliwili-ai-'+VERSION+'-source',dist/('wiliwili-ai-'+VERSION+'-source.tar.zst'))
    (dist/'SHA256SUMS').write_text(''.join(digest(p)+'  '+p.name+'\n' for p in sorted(dist.glob('*.tar.zst'))))
    (dist/'build-info.json').write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'version':VERSION,'artifacts':[str(p) for p in sorted(dist.iterdir())]},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
