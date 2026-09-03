"""DNS-over-HTTPS resolution.

Some networks filter specific hostnames at the resolver. The endpoints this
tool needs are ``*.run.app``; anyone can stand up a Cloud Run service in
seconds, so the domain attracts abuse and lands on blocklists, and filtering
resolvers -- consumer routers, Pi-hole, company DNS -- return nothing for it.
The name is fine; the resolver refuses to say so.

When enabled, this module asks a public resolver over HTTPS instead, then
connects straight to the returned address. TLS is unchanged: the certificate
is still validated against the real hostname via SNI, so nothing is trusted
that would not have been trusted before. Only the lookup path differs.

This is opt-in on purpose. A resolver blocking a name may be a misfiring
blocklist, or may be deliberate policy on a managed machine, and a tool that
quietly defeats it is doing something the user did not ask for.
"""

from __future__ import annotations

import http.client
import json
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

#: Public DoH endpoints, addressed by IP so no bootstrap lookup is needed --
#: resolving the resolver's own name would hit the very problem we are routing
#: around. Both present certificates valid for these IP addresses.
DOH_ENDPOINTS = (
    "https://1.1.1.1/dns-query",
    "https://9.9.9.9/dns-query",
)

_QUERY_TIMEOUT = 5
_cache: dict[str, list[str]] = {}


class ResolverError(Exception):
    """Raised when a hostname could not be resolved by any means."""


def _query_doh(endpoint: str, hostname: str, record: str = "A") -> list[str]:
    """Ask one DoH endpoint for a hostname's addresses."""
    url = f"{endpoint}?{urllib.parse.urlencode({'name': hostname, 'type': record})}"
    request = urllib.request.Request(
        url, headers={"accept": "application/dns-json", "User-Agent": "agup"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_QUERY_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return []

    if not isinstance(payload, dict) or payload.get("Status") != 0:
        return []

    wanted = 1 if record == "A" else 28
    answers = payload.get("Answer")
    if not isinstance(answers, list):
        return []

    addresses = []
    for answer in answers:
        if not isinstance(answer, dict) or answer.get("type") != wanted:
            continue
        data = answer.get("data")
        if isinstance(data, str) and data:
            addresses.append(data)
    return addresses


def resolve(hostname: str, *, use_cache: bool = True) -> list[str]:
    """Resolve a hostname over DoH, preferring IPv4.

    IPv4 first because a host may advertise AAAA records while having no
    working IPv6 route, in which case connecting to them hangs.
    """
    if use_cache and hostname in _cache:
        return _cache[hostname]

    for endpoint in DOH_ENDPOINTS:
        for record in ("A", "AAAA"):
            addresses = _query_doh(endpoint, hostname, record)
            if addresses:
                _cache[hostname] = addresses
                return addresses

    raise ResolverError(
        f"DoH resolution failed for {hostname}. The name could not be resolved "
        f"by the system resolver or by public DNS-over-HTTPS; the network may "
        f"be blocking more than DNS."
    )


def system_can_resolve(hostname: str) -> bool:
    """Whether the system resolver returns anything for a hostname."""
    try:
        socket.getaddrinfo(hostname, None)
        return True
    except socket.gaierror:
        return False


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection to a fixed address, with SNI for the real hostname.

    The socket is opened to an address we resolved ourselves, while the TLS
    handshake and certificate validation still use the original hostname. A
    wrong or hostile address therefore fails the certificate check exactly as
    it would have before.
    """

    def __init__(self, host: str, address: str, **kwargs) -> None:
        self._real_host = host
        self._address = address
        super().__init__(host, **kwargs)

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._address, self.port or 443), self.timeout
        )
        if self._tunnel_host:
            self._tunnel()
        context = self._context or ssl.create_default_context()
        # server_hostname drives both SNI and certificate verification.
        self.sock = context.wrap_socket(self.sock, server_hostname=self._real_host)


class DoHHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler that resolves over DoH when the system resolver cannot.

    The system resolver stays in charge whenever it works, so a deliberately
    configured resolver keeps deciding. DoH is used only for names it fails to
    resolve at all, and the caller is told when that happens.
    """

    def __init__(self, on_fallback=None, always: bool = False) -> None:
        super().__init__()
        self._on_fallback = on_fallback
        self._always = always

    def https_open(self, req):
        return self.do_open(self._build, req)

    def _build(self, host: str, **kwargs):
        hostname = host.split(":")[0]
        port = int(host.split(":")[1]) if ":" in host else 443

        if not self._always and system_can_resolve(hostname):
            kwargs.pop("context", None)
            return http.client.HTTPSConnection(
                hostname, port=port, context=ssl.create_default_context(), **kwargs
            )

        first_time = hostname not in _cache
        addresses = resolve(hostname)
        if first_time and self._on_fallback:
            self._on_fallback(hostname, addresses[0])

        kwargs.pop("context", None)
        return _PinnedHTTPSConnection(
            hostname,
            addresses[0],
            port=port,
            context=ssl.create_default_context(),
            **kwargs,
        )


def build_opener(on_fallback=None, always: bool = False) -> urllib.request.OpenerDirector:
    """An opener that falls back to DoH when system resolution fails."""
    return urllib.request.build_opener(DoHHTTPSHandler(on_fallback, always))


def clear_cache() -> None:
    _cache.clear()
