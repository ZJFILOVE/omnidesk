"""Small valid PDF fixtures, including xref, for actual Poppler rendering."""
from pathlib import Path


def write_pdf(path: Path, *, pages: int = 1, title: str = "", author: str = "") -> Path:
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", ("<< /Type /Pages /Kids [" + " ".join(f"{i+4} 0 R" for i in range(pages)) + f"] /Count {pages} >>").encode(), f"<< /Title ({title}) /Author ({author}) >>".encode()]
    objects.extend(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 400] /Resources << >> >>" for _ in range(pages))
    raw = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(raw))
        raw.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(raw)
    raw.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        raw.extend(f"{offset:010} 00000 n \n".encode())
    raw.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R /Info 3 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path
