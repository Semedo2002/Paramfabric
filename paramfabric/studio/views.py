"""HTML renderer for the protected Studio shell"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi.responses import HTMLResponse


def studio_response() -> HTMLResponse:
    """Render the bundled Studio UI with a restrictive same-origin policy"""
    nonce=secrets.token_urlsafe(18)
    html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    response = HTMLResponse(html)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
    )
    return response
