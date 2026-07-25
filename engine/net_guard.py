"""
engine/net_guard.py

Egress boundary for the research fetcher.

`researcher._fetch_text()` fetches URLs that ultimately come from search
results and from pages we crawled — i.e. from parties who are not us. An SSRF
there turns this process into an unauthenticated scanner and proxy for hosts
the crawled site could never reach on its own: RFC1918 space, cloud metadata
endpoints, and any other host routable from wherever this runs.

Two rules, both necessary:

  1. Resolve the hostname and check the *resolved addresses*. Validating the
     URL string alone is defeated by DNS — a perfectly ordinary-looking
     domain can simply have an A record pointing at 127.0.0.1 or 10.x.
  2. Never auto-follow redirects. A public first hop that 302s to
     169.254.169.254 passes rule 1 and then lands wherever it likes, so each
     hop is re-validated explicitly by the caller.

Fails closed throughout: an unresolvable host, an unexpected scheme, or a
resolution error is a refusal, not a licence to connect.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")

# CGNAT space (100.64.0.0/10). Python's `is_private` does not treat this as
# private, but it is not publicly routable either and is commonly used for
# private overlay networking, so it is excluded explicitly.
_CGNAT_V4 = ipaddress.ip_network("100.64.0.0/10")

MAX_REDIRECT_HOPS = 3


def is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for globally-routable unicast addresses."""
    if ip.version == 4 and ip in _CGNAT_V4:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolve(host: str) -> list:
    """Resolve a hostname to every address it maps to. Separate function so
    tests can substitute it without touching the network."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [ipaddress.ip_address(info[4][0]) for info in infos]


def is_safe_url(url: str) -> tuple[bool, str]:
    """Return (safe, reason). Safe means: an allowed scheme, a resolvable
    host, and *every* address it resolves to is publicly routable.

    All addresses are checked, not just the first — a host that returns both
    a public and a private A record must not be reachable via the private one
    on a retry.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return False, f"unparseable URL: {exc}"

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False, f"scheme {parsed.scheme!r} is not allowed (only http/https)"

    host = parsed.hostname
    if not host:
        return False, "URL has no host"

    # A literal IP in the URL still goes through the same check below.
    try:
        addresses = _resolve(host)
    except Exception as exc:
        return False, f"could not resolve {host!r} ({exc}) — refusing to connect"

    if not addresses:
        return False, f"{host!r} resolved to no addresses"

    for ip in addresses:
        if not is_public_ip(ip):
            return False, f"{host!r} resolves to {ip}, which is not a public address (private/loopback/link-local/CGNAT)"

    return True, "ok"


def safe_get(client, url: str, headers: dict | None = None, max_hops: int = MAX_REDIRECT_HOPS):
    """GET `url` with redirects followed manually, re-validating every hop.

    `client` is an httpx.Client constructed with follow_redirects=False.
    Returns the final response, or None if any hop fails validation.
    """
    current = url
    for hop in range(max_hops + 1):
        ok, reason = is_safe_url(current)
        if not ok:
            logger.warning(f"[net_guard] Blocked fetch of {current!r}: {reason}")
            return None

        resp = client.get(current, headers=headers or {})
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp

        location = resp.headers.get("location")
        if not location:
            return resp
        # Resolve relative redirects against the current URL before checking.
        from urllib.parse import urljoin
        current = urljoin(current, location)

    logger.warning(f"[net_guard] Too many redirects starting from {url!r}")
    return None
