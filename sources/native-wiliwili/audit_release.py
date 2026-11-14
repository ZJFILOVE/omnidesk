#!/usr/bin/env python3
"""Check known private credentials against source/evidence and both archives.

Only filenames and pass/fail booleans are emitted. Public application signing
constants are intentionally not classified as account credentials.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
from urllib.parse import quote

ROOT=Path(__file__).resolve().parent


def main():
    secrets=set()
    home=Path.home()
    for path in [home/'.config/wiliwili/android-session.json',home/'.config/wiliwili/wiliwili_config.json',home/'.config/bili-helper-codex/config.json']:
        if not path.exists():continue
        data=json.loads(path.read_text())
        def collect(value, sensitive=False):
            if isinstance(value,dict):
                for k,v in value.items():collect(v,sensitive or k.lower() in ('cookie','cookies','access_token','refresh_token','refreshtoken','token','sessdata','bili_jct','dedeuserid__ckmd5'))
            elif isinstance(value,list):
                for v in value:collect(v,sensitive)
            elif sensitive and isinstance(value,str) and len(value)>=16:
                secrets.add(value.encode());secrets.add(quote(value,safe='').encode())
        collect(data)
    findings=[]; scanned=0
    def check(data,name):
        nonlocal scanned
        scanned+=1
        if any(s in data for s in secrets):findings.append(name)
    for path in ROOT.rglob('*'):
        if any(part in ('upstream','build','toolchain','dist','.git','__pycache__') for part in path.relative_to(ROOT).parts):continue
        if path.is_file() and not path.is_symlink():check(path.read_bytes(),str(path.relative_to(ROOT)))
    tracked=subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard','-z'],cwd=ROOT.parent).decode().split('\0')
    for name in tracked:
        path=ROOT.parent/name
        if name and path.is_file() and not path.is_symlink() and 'native-wiliwili/' not in name:
            check(path.read_bytes(),name)
    forbidden=[]; archives=[]
    for path in sorted((ROOT/'dist').glob('*.tar.zst')):
        with tempfile.NamedTemporaryFile(suffix='.tar') as tarpath:
            subprocess.run(['zstd','-q','-d','-f',str(path),'-o',tarpath.name],check=True)
            with tarfile.open(tarpath.name) as tar:
                members=tar.getmembers()
                for item in members:
                    if any(x in item.name for x in ('android-session.json','app-session.json','app-qr.json','android-lab/','validation/','.git/','client.log','.mp4','.bin')):forbidden.append(item.name)
                    if item.isfile():check(tar.extractfile(item).read(),path.name+':'+item.name)
                archives.append({'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'members':len(members)})
    result={'evidence':'actual_source_and_release_scan','known_credentials_scanned':bool(secrets),'files_scanned':scanned,
            'credential_findings':findings,'excluded_file_findings':forbidden,'archives':archives,'passed':not findings and not forbidden}
    (ROOT/'validation/release-3.0-secret-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
