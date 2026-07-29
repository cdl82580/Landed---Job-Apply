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


def resolve_safe_ip(url: str) -> str | None:
    """Resolve url's hostname once and return a single safe IP to connect to,
    or None if it doesn't resolve, isn't http(s), or any resolved address is
    private/loopback/link-local/reserved/multicast/unspecified.

    Pair this with post_pinned() so the exact address validated here is the
    one actually connected to. A separate "check with is_ssrf_url(), then
    let the HTTP client re-resolve and connect" two-step leaves a DNS-
    rebinding gap: a short-TTL or multi-answer DNS response can return a
    different (unvalidated) address on the second lookup moments later.
    Resolving once and pinning the connection to that address closes it.
    """
    try:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return None
        host  = parsed.hostname or ""
        addrs = socket.getaddrinfo(host, None)
        ips: list[str] = []
        for _, _, _, _, sockaddr in addrs:
            ip = ipaddress.ip_address(sockaddr[0])
            if _is_blocked_ip(ip):
                return None
            ips.append(sockaddr[0])
        return ips[0] if ips else None
    except Exception:
        return None


def _make_pinned_adapter(resolved_ip: str):
    """Build a fresh requests.HTTPAdapter subclass instance that routes its
    connection to resolved_ip instead of re-resolving the request's hostname,
    while keeping the original hostname for the Host header and (for HTTPS)
    TLS SNI + certificate hostname verification. A new instance per call —
    never shared or mounted globally — so concurrent deliveries in other
    threads can't race on which IP is pinned. This overrides
    get_connection_with_tls_context(), requests' own documented extension
    point for exactly this use case (see HTTPAdapter.build_connection_pool_key_attributes).
    """
    from requests.adapters import HTTPAdapter

    class _PinnedIPAdapter(HTTPAdapter):
        def send(self, request, **kwargs):
            # Once host_params["host"] below is overridden to the pinned IP,
            # urllib3 auto-derives the Host header from that same value —
            # it would otherwise send "Host: <ip>" instead of the real
            # hostname, breaking any name-based virtual hosting (Cloudflare,
            # shared load balancers, etc.) on the other end.
            request.headers.setdefault("Host", urllib.parse.urlparse(request.url).hostname)
            return super().send(request, **kwargs)

        def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
            host_params, pool_kwargs = self.build_connection_pool_key_attributes(request, verify, cert)
            original_host = host_params.get("host")
            host_params["host"] = resolved_ip
            if host_params.get("scheme") == "https":
                pool_kwargs["assert_hostname"] = original_host
                pool_kwargs["server_hostname"] = original_host
            return self.poolmanager.connection_from_host(**host_params, pool_kwargs=pool_kwargs)

    return _PinnedIPAdapter()


def post_pinned(url: str, resolved_ip: str, **kwargs):
    """requests.post(url, **kwargs), but the connection is made directly to
    resolved_ip instead of re-resolving url's hostname — see resolve_safe_ip()."""
    import requests
    session = requests.Session()
    adapter = _make_pinned_adapter(resolved_ip)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    try:
        return session.post(url, **kwargs)
    finally:
        session.close()
