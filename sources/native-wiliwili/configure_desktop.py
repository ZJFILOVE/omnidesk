#!/usr/bin/env python3
"""Apply desktop density after the client exits, preserving every other setting."""
import json
import os
from pathlib import Path
import shutil

root = Path(__file__).resolve().parent
config = Path.home()/'.config/wiliwili/wiliwili_config.json'
installation = json.loads((Path.home()/'.local/opt/wiliwili-ai/installation.json').read_text())
backup = Path(installation['backup'])/'wiliwili_config.json'
content = json.loads(config.read_text()) if config.exists() else {}
settings = content.setdefault('setting', {})
if not isinstance(settings, dict): raise SystemExit('Unexpected settings structure; configuration unchanged')
if config.exists() and not backup.exists():
    shutil.copy2(config, backup)
    backup.chmod(0o600)
settings['app_ui_scale'] = '1080p'
settings['search_tv_mode'] = False
config.parent.mkdir(parents=True, exist_ok=True)
temporary = config.with_suffix('.json.ui-tmp')
temporary.write_text(json.dumps(content,ensure_ascii=False,indent=2)+'\n')
temporary.chmod(0o600)
os.replace(temporary, config)
print('Desktop density: 1080p; television search disabled; existing settings preserved')
