"""
scripts/ssrf.py — Shared SSRF guard for outbound-URL features (webhooks, etc).

Used both at admin-configuration time and again at delivery time (to guard
against DNS rebinding between the two checks).
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse

# Ranges the stdlib's is_private/is_reserved/etc. classifications do NOT
# catch. Confirmed: ipaddress.ip_address("100.64.0.1").is_private is False.
_EXTRA_BLOCKED_NETWORKS = [
    ipaddress.ip_network("100.64.0.0/10"),  # RFC 6598 carrier-grade NAT
]


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True if ip is loopback/private/link-local/reserved/multicast/unspecified.

    Unwraps IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1) to their IPv4
    form first — otherwise they'd be evaluated as plain IPv6 and could slip
    past the equivalent IPv4 block (is_private etc. don't follow the mapping
    automatically). Uses stdlib's built-in classifications rather than a
    hand-maintained CIDR list, which also covers ranges like 0.0.0.0/8 and
    the IPv6 documentation/mapped ranges that a short manual list would miss
    — plus an explicit check for CGNAT space (100.64.0.0/10), which the
    stdlib classifies as public despite being internal-only in practice.
    """
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    ):
        return True
    return isinstance(ip, ipaddress.IPv4Address) and any(
        ip in net for net in _EXTRA_BLOCKED_NETWORKS
    )


def is_ssrf_url(url: str) -> bool:
    """Return True if the URL resolves to a private/loopback/link-local address."""
    try:
        parsed = urllib.parse.urlparse(url)
        host   = parsed.hostname or ""
        addrs  = socket.getaddrinfo(host, None)
        for _, _, _, _, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if _is_blocked_ip(ip):
                return True
    except Exception:
        pass
    return False
