"""Input rules and URL spelling for news sources.

No third-party imports: the store and status paths use these helpers and must
stay cheap to import.  Error text never contains the offending input, because
a source URL can carry a secret in its path or query.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

MAX_URL_CHARS = 2048
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_BAD_URL = "URL の形式が正しくありません"


class NewsInputError(ValueError):
    """Input is invalid; the message is fixed and never echoes the input."""


def validate_name(name: str) -> None:
    """Accept the grammar for new sources; raise NewsInputError otherwise."""
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        raise NewsInputError("名前の形式が正しくありません")


def validate_url(url: str) -> None:
    """Accept http/https with a host, no userinfo, no '#', at most 2048 chars.

    The length is that of the input as given.  Raises NewsInputError whose text
    does not contain the URL.
    """
    if not isinstance(url, str) or len(url) > MAX_URL_CHARS:
        raise NewsInputError(_BAD_URL)
    try:
        parts = urlsplit(url)
        host = parts.hostname
        parts.port  # noqa: B018 - raises ValueError for an unusable port
    except ValueError:
        raise NewsInputError(_BAD_URL) from None
    if (parts.scheme.lower() not in {"http", "https"} or not host
            or parts.username is not None or parts.password is not None
            or "#" in url):
        raise NewsInputError(_BAD_URL)


def normalize_url(url: str) -> str:
    """Lower-case scheme and host, drop the default port, keep path and query.

    A trailing empty '?' is kept as written (urlunsplit would drop it).
    Raises NewsInputError for anything validate_url rejects.
    """
    validate_url(url)
    p = urlsplit(url)
    scheme = p.scheme.lower()
    host = p.hostname.lower()  # type: ignore[union-attr]
    if ":" in host:
        host = f"[{host}]"
    port = p.port
    if port is not None and not ((scheme == "http" and port == 80)
                                 or (scheme == "https" and port == 443)):
        host += f":{port}"
    out = urlunsplit((scheme, host, p.path, p.query, ""))
    if url.endswith("?") and not out.endswith("?"):
        out += "?"
    return out


def fold_url(url: str) -> str:
    """Return '<scheme>://<host>/…' (a non-default port is kept); no path or query."""
    p = urlsplit(normalize_url(url))
    return f"{p.scheme}://{p.netloc}/…"
