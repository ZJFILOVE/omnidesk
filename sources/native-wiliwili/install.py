#!/usr/bin/env python3
"""Install/upgrade the complete release, retaining the replaced installation."""
import argparse
import datetime
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

VERSION = '3.0.0'
RUNTIME = ['service.py', 'launch.py', 'bridge.py', 'LICENSE', 'README.md', 'CHANGELOG.md', 'build-info.json']


def payload(source, destination):
    """An explicit allowlist keeps research, private configs and fixtures out."""
    source = Path(source)
    binary = source/'bin/wiliwili' if (source/'bin/wiliwili').exists() else source/'build/wiliwili'
    resources = source/'resources' if (source/'resources').exists() else source/'build/resources'
    if not binary.is_file() or not resources.is_dir(): raise SystemExit('请先完整构建应用，或使用发行包内的安装器')
    (destination/'bin').mkdir(parents=True)
    shutil.copy2(binary, destination/'bin/wiliwili')
    shutil.copytree(resources, destination/'resources')
    for name in RUNTIME:
        original = source/name
        if name == 'bridge.py' and not original.exists(): original = source.parent/'local-codex-bridge/server.py'
        if not original.is_file(): raise SystemExit('发行文件缺失：'+name)
        shutil.copy2(original, destination/name)
    if (source/'licenses').exists(): shutil.copytree(source/'licenses', destination/'licenses')
    (destination/'bin/wiliwili').chmod(0o755)


def atomic_text(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name+'.new')
    pending.write_text(content); pending.chmod(mode); pending.replace(path)


def install(args):
    source = args.source.resolve(); dest = args.prefix.resolve()
    launcher = args.bin_dir/'wiliwili-ai'; desktop = args.desktop_dir/'wiliwili-ai.desktop'
    date = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f'); backup = args.backup_dir/date
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.'+dest.name+'-upgrade-',dir=dest.parent) as temporary:
        staged = Path(temporary)/'app'; payload(source, staged)
        subprocess.run([str(staged/'bin/wiliwili'), '--version'], check=True, stdout=subprocess.DEVNULL)
        backup.mkdir(parents=True, exist_ok=True); manifest = {}
        for path in [dest, launcher, desktop]:
            if path.exists() or path.is_symlink():
                target = backup/path.name
                if path == dest: path.rename(target)
                else: shutil.copy2(path, target, follow_symlinks=False)
                manifest[str(path)] = str(target)
        (backup/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
        try: staged.rename(dest)
        except BaseException:
            if str(dest) in manifest: Path(manifest[str(dest)]).rename(dest)
            raise
    atomic_text(launcher,'#!/usr/bin/python3\nimport runpy\nrunpy.run_path('+repr(str(dest/'launch.py'))+',run_name="__main__")\n',0o755)
    atomic_text(desktop,'[Desktop Entry]\nType=Application\nName=wiliwili AI\nComment=B站原生客户端 · 原生登录、高清播放与本机 AI\nExec='+str(launcher)+'\nIcon='+str(dest/'resources/icon/icon.png')+'\nTerminal=false\nCategories=AudioVideo;Video;\nStartupWMClass=wiliwili\n',0o644)
    result = dict(version=VERSION,installed=str(dest),launcher=str(launcher),desktop=str(desktop),backup=str(backup),
                  previous_installations=list(manifest),upstream_version='1.6.0',build=json.loads((dest/'build-info.json').read_text()))
    (dest/'installation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    if args.report: args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='build'},ensure_ascii=False,indent=2))


if __name__=='__main__':
    home=Path.home(); parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parent)
    parser.add_argument('--prefix',type=Path,default=home/'.local/opt/wiliwili-ai')
    parser.add_argument('--bin-dir',type=Path,default=home/'.local/bin')
    parser.add_argument('--desktop-dir',type=Path,default=home/'.local/share/applications')
    parser.add_argument('--backup-dir',type=Path,default=home/'.local/share/wiliwili-ai/backups')
    parser.add_argument('--report',type=Path)
    install(parser.parse_args())
