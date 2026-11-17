"""Safe image Markdown shared by untrusted HTML and comment adapters."""
from urllib.parse import urljoin, urlsplit


def image_markdown(url: str, alt: str = "图片", *, base_url: str = "") -> str:
    resolved = urljoin(base_url, url.strip())
    try:
        parts = urlsplit(resolved)
        if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
            return "[图片]"
    except ValueError:
        return "[图片]"
    label = (alt or "图片").replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]").replace("\n", " ").replace("\r", " ")
    safe = resolved.replace("<", "%3C").replace(">", "%3E").replace("\n", "%0A").replace("\r", "%0D").replace(" ", "%20")
    return f"![{label}](<{safe}>)"
