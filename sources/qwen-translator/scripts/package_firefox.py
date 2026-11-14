"""Build a reproducible, unsigned Firefox archive with no local credentials."""
from pathlib import Path
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
FILES = ('manifest.json', 'cache.js', 'background.js', 'content.js', 'popup.html', 'popup.js',
         'options.html', 'options.js', 'shortcuts.js', 'ui.css', 'SOURCES.md')


def build():
    version = json.loads((ROOT / 'firefox-extension/manifest.json').read_text())['version']
    destination = ROOT / f'dist/qwen-firefox-{version}-unsigned.xpi'
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name in (*FILES, *sorted(p.relative_to(ROOT / "firefox-extension").as_posix()
                                    for p in (ROOT / "firefox-extension/icons").glob("*")
                                    if p.suffix in {".svg", ".png"})):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o644 << 16
            archive.writestr(entry, (ROOT / 'firefox-extension' / name).read_bytes())
    print(destination)
    return destination


if __name__ == '__main__':
    build()
