"""Docker URL rewriting for FROST @iot.nextLink pagination.

Solves the container networking mismatch where FROST returns
@iot.nextLink URLs pointing to the public hostname, but the
Python service running inside Docker needs the internal address.
"""

from __future__ import annotations


def rewrite_url(url: str, public_base: str, internal_base: str) -> str:
    """Replace the public base URL in *url* with the internal one.

    If *url* does not start with *public_base*, it is returned unchanged.
    Both bases are compared after stripping trailing slashes.
    """
    if not public_base or not internal_base:
        return url
    pub = public_base.rstrip("/")
    internal = internal_base.rstrip("/")
    if url.startswith(pub):
        return internal + url[len(pub):]
    return url
