"""Safe WeasyPrint rendering (day-1 audit finding 3 + 45).

Every PDF this backend builds comes from an HTML string we assemble
ourselves. WeasyPrint's default URL fetcher follows ANY url it finds in
that HTML: file:// (server files such as /proc/self/environ or .env),
http:// (internal / cloud-metadata addresses) and so on. If a user
manages to get markup into a report (for example
`<a rel=attachment href=file:///proc/self/environ>`), WeasyPrint would
read that file and embed it in the PDF.

Our reports only ever need inline `data:` URIs (photo thumbnails are
already inlined as base64), so the fetcher here allows `data:` and
refuses everything else with ValueError. WeasyPrint catches fetcher
errors per resource and simply leaves that resource out, so a refused
URL never breaks the export.

`render_pdf` is the one place PDFs get rendered. `arender_pdf` is the
async twin: WeasyPrint layout is CPU-heavy and blocking, so async route
handlers must run it in a worker thread instead of on the event loop
(otherwise one export freezes the server for every user).
"""
import asyncio
import functools
import re
import threading
from concurrent.futures import ThreadPoolExecutor

_SCHEME_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9+.\-]*):")
_ALLOWED_SCHEMES = frozenset({"data"})

# WeasyPrint / Pango thread-safety across concurrent renders is not
# guaranteed, so renders are serialized. Exports are rare; this only
# makes a second simultaneous export wait in its worker thread. The
# event loop itself is never held.
_RENDER_LOCK = threading.Lock()
_PDF_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdf-render")


def _scheme_of(url) -> str:
    m = _SCHEME_RE.match(str(url or ""))
    return m.group(1).lower() if m else ""


def safe_url_fetcher(url, *args, **kwargs):
    """WeasyPrint url_fetcher that only serves `data:` URLs.

    Raises ValueError for file:, http:, https:, ftp: and anything else
    (including relative paths and Windows drive paths like C:/...)."""
    scheme = _scheme_of(url)
    if scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"PDF export blocked a non-inline resource ({scheme or 'no scheme'})")
    try:
        # WeasyPrint >= 68: URLFetcher is the supported API;
        # default_url_fetcher is deprecated there (and gone in 69).
        from weasyprint import URLFetcher
    except ImportError:  # pragma: no cover - older WeasyPrint
        from weasyprint import default_url_fetcher
        return default_url_fetcher(url, *args, **kwargs)
    return URLFetcher(allowed_protocols=set(_ALLOWED_SCHEMES)).fetch(url)


def safe_css(css_string: str):
    """weasyprint.CSS built with the safe fetcher (for @import / url()
    inside extra stylesheets passed to render_pdf)."""
    from weasyprint import CSS
    return CSS(string=css_string, url_fetcher=safe_url_fetcher)


def render_pdf(html_string: str, **write_pdf_kwargs) -> bytes:
    """Render an HTML string to PDF bytes with outside fetches refused."""
    from weasyprint import HTML
    with _RENDER_LOCK:
        return HTML(string=html_string, url_fetcher=safe_url_fetcher).write_pdf(
            **write_pdf_kwargs,
        )


async def arender_pdf(html_string: str, **write_pdf_kwargs) -> bytes:
    """render_pdf in a worker thread so the event loop stays free.

    Uses its own one-thread pool rather than the shared default pool:
    renders are serialized by _RENDER_LOCK anyway, and queued exports
    must not sit in shared worker threads that photo / voice storage
    calls (aput_object / aget_object) also need."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _PDF_EXECUTOR,
        functools.partial(render_pdf, html_string, **write_pdf_kwargs),
    )
