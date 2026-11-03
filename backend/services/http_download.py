"""Download header helper.

Response headers are latin-1, so a filename built from a user-typed name (a
project called "Renovate 6B – Building 100" with an en dash) must never reach
the header raw: the server crashes with a bare 500 *after* the file is built.
This gives a plain-ASCII filename plus the RFC 5987 UTF-8 form, so browsers
still show the real name."""
import re
import unicodedata
from urllib.parse import quote


def attachment_disposition(filename: str, fallback: str = "download") -> str:
    name = "".join(c for c in str(filename or "") if ord(c) >= 32 and c not in '"\\').strip()
    if not name:
        name = fallback
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    ascii_name = re.sub(r"\s+", " ", ascii_name).strip() or fallback
    if ascii_name == name:
        return f'attachment; filename="{ascii_name}"'
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(name, safe='')}"
