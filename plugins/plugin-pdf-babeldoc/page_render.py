"""Bounded local PDF page rasterization; no translated artifact is required."""
from __future__ import annotations

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path


def render_page(path: Path, page: int, *, cover: bool = False) -> str:
    binary = shutil.which("pdftoppm")
    if not binary:
        raise RuntimeError("显示 PDF 需要 Poppler 的 pdftoppm，请安装 poppler 后重试")
    if page < 1 or path.is_symlink() or not path.is_file():
        raise ValueError("Invalid PDF page or file")
    with tempfile.TemporaryDirectory(prefix="omnidesk-pdf-page-") as directory:
        prefix = Path(directory) / "page"
        result = subprocess.run(
            [binary, "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "360" if cover else "1600", "-png", str(path), str(prefix)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=15, check=False,
        )
        image = prefix.with_suffix(".png")
        if result.returncode or not image.is_file():
            raise RuntimeError("PDF 页面渲染失败，文件可能已损坏或受密码保护")
        if image.stat().st_size > 8 * 1024 * 1024:
            raise RuntimeError("PDF 页面图片超过显示大小限制")
        return "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode("ascii")
