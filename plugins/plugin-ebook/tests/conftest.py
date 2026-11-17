"""Shared offline pytest fixtures and EPUB/plugin test helpers."""

from __future__ import annotations

import importlib.util
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = PLUGIN_DIR.parents[1]

for _p in (str(REPO_ROOT), str(REPO_ROOT / "core"), str(PLUGIN_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _isolate_xdg_and_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure tests never read or write the user's real home or XDG directories."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg-run"))


def load_plugin_module() -> Any:
    """Load plugin.py directly for isolated unit tests."""
    spec = importlib.util.spec_from_file_location("plugin_ebook_under_test", PLUGIN_DIR / "plugin.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_minimal_epub(
    dest: Path,
    *,
    title: str = "Test EPUB Book",
    author: str = "Ada Lovelace",
    language: str = "en",
    chapters: list[dict[str, Any]] | None = None,
    use_nav: bool = True,
    use_ncx: bool = True,
    extra_entries: dict[str, bytes] | None = None,
) -> Path:
    """Create a valid minimal EPUB 2/3 ZIP archive in `tmp_path`."""
    chapters = chapters or [
        {
            "filename": "ch1.xhtml",
            "title": "Chapter One: Origins",
            "body": (
                "<h1>Chapter One: Origins</h1>"
                "<p>An ephemeral design favors clarity over ceremony.</p>"
                "<p>Although systems evolve, idempotent contracts remain resilient.</p>"
                '<img src="images/diagram.png" alt="Architecture Diagram"/>'
            ),
        },
        {
            "filename": "ch2.xhtml",
            "title": "Chapter Two: Telemetry",
            "body": (
                "<h1>Chapter Two: Telemetry</h1>"
                "<p>Telemetry from the floor sounded reassuring, yet left an epistemic gap.</p>"
            ),
        },
    ]

    manifest_items = []
    spine_items = []
    nav_links = []
    ncx_points = []

    for idx, ch in enumerate(chapters, start=1):
        fname = ch["filename"]
        item_id = f"ch{idx}"
        manifest_items.append(f'<item id="{item_id}" href="{fname}" media-type="application/xhtml+xml"/>')
        spine_items.append(f'<itemref idref="{item_id}"/>')
        nav_links.append(f'<li><a href="{fname}">{ch["title"]}</a></li>')
        ncx_points.append(
            f'<navPoint id="np-{idx}" playOrder="{idx}">'
            f'<navLabel><text>{ch["title"]}</text></navLabel>'
            f'<content src="{fname}"/></navPoint>'
        )

    if use_nav:
        manifest_items.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    if use_ncx:
        manifest_items.append('<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>')

    spine_attr = ' toc="ncx"' if use_ncx else ""
    opf_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f'<dc:identifier id="uid">urn:uuid:test-ebook</dc:identifier>'
        f'<dc:title>{title}</dc:title>'
        f'<dc:creator>{author}</dc:creator>'
        f'<dc:language>{language}</dc:language>'
        '</metadata>'
        f'<manifest>{"".join(manifest_items)}</manifest>'
        f'<spine{spine_attr}>{"".join(spine_items)}</spine>'
        '</package>'
    )
    container_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>'
        '</container>'
    )
    nav_xhtml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
        '<head><title>Table of Contents</title></head>'
        f'<body><nav epub:type="toc"><ol>{"".join(nav_links)}</ol></nav></body>'
        '</html>'
    )
    ncx_xml = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">'
        f'<head><meta name="dtb:uid" content="urn:uuid:test-ebook"/></head>'
        f'<docTitle><text>{title}</text></docTitle>'
        f'<navMap>{"".join(ncx_points)}</navMap>'
        '</ncx>'
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        zf.writestr("META-INF/container.xml", container_xml.encode("utf-8"))
        zf.writestr("OEBPS/content.opf", opf_xml.encode("utf-8"))
        if use_nav:
            zf.writestr("OEBPS/nav.xhtml", nav_xhtml.encode("utf-8"))
        if use_ncx:
            zf.writestr("OEBPS/toc.ncx", ncx_xml.encode("utf-8"))
        for ch in chapters:
            html_doc = (
                '<?xml version="1.0" encoding="utf-8"?>'
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                f'<head><title>{ch["title"]}</title></head>'
                f'<body>{ch["body"]}</body></html>'
            )
            zf.writestr(f'OEBPS/{ch["filename"]}', html_doc.encode("utf-8"))
        if extra_entries:
            for member_name, data in extra_entries.items():
                zf.writestr(member_name, data)
    return dest
