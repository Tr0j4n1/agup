"""DoH resolution and TLS validation.

The checks that matter here are the TLS ones. --doh-only and the automatic
fallback connect to an address we resolved ourselves, so certificate
validation is the only thing standing between the user and a hostile answer.
It must remain exact.

Network-dependent tests skip cleanly when there is no connectivity, and warn
rather than pass when TLS is being intercepted -- behind a proxy that mints
its own certificates these assertions prove nothing.
"""

import socket
import ssl
import sys

sys.path.insert(0, "src")

from agup import resolver as R  # noqa: E402
from agup.resolver import _PinnedHTTPSConnection, DoHHTTPSHandler  # noqa: E402

FAILS = []
PROBE = "api.github.com"


def check(name, fn):
    try:
        fn()
        print(f"  ok   {name}")
    except _Skip as err:
        print(f"  skip {name}: {err}")
    except Exception as err:  # noqa: BLE001
        FAILS.append(name)
        print(f"  FAIL {name}: {type(err).__name__}: {err}")


class _Skip(Exception):
    pass


def _online():
    try:
        socket.getaddrinfo(PROBE, 443)
        return True
    except socket.gaierror:
        return False


def _probe_ip():
    if not _online():
        raise _Skip("no network")
    return socket.getaddrinfo(PROBE, 443, socket.AF_INET)[0][4][0]


def _intercepted():
    """Whether TLS is being intercepted, making cert checks meaningless."""
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(
            socket.create_connection((PROBE, 443), timeout=15), server_hostname=PROBE
        ) as sock:
            issuer = dict(x[0] for x in sock.getpeercert()["issuer"])
    except OSError:
        return None
    known = {"DigiCert Inc", "Sectigo Limited", "Let's Encrypt", "Google Trust Services LLC"}
    return issuer.get("organizationName", "?") not in known


# --- offline: parsing, cache, ordering ---------------------------------------

print("doh lookup")


def t_ipv4_first():
    calls = []

    def fake(endpoint, hostname, record="A"):
        calls.append(record)
        return ["34.143.76.2"] if record == "A" else ["2600::1"]

    orig, R._query_doh = R._query_doh, fake
    R.clear_cache()
    try:
        assert R.resolve("x.run.app") == ["34.143.76.2"]
        assert calls[0] == "A", "IPv4 must be tried first"
    finally:
        R._query_doh = orig
        R.clear_cache()


def t_aaaa_fallback():
    orig = R._query_doh
    R._query_doh = lambda e, h, record="A": [] if record == "A" else ["2600::1"]
    R.clear_cache()
    try:
        assert R.resolve("x.run.app") == ["2600::1"]
    finally:
        R._query_doh = orig
        R.clear_cache()


def t_endpoints_are_ip_literals():
    import re

    for endpoint in R.DOH_ENDPOINTS:
        host = endpoint.split("//")[1].split("/")[0]
        assert re.fullmatch(r"[0-9.]+", host), f"{endpoint} needs a bootstrap lookup"


def t_all_fail_raises():
    orig, R._query_doh = R._query_doh, lambda *a, **k: []
    R.clear_cache()
    try:
        try:
            R.resolve("x.run.app")
            raise AssertionError("should have raised")
        except R.ResolverError as err:
            assert "blocking more than DNS" in str(err)
    finally:
        R._query_doh = orig
        R.clear_cache()


check("IPv4 preferred over IPv6", t_ipv4_first)
check("AAAA used when no A records", t_aaaa_fallback)
check("DoH endpoints need no bootstrap lookup", t_endpoints_are_ip_literals)
check("exhausted endpoints raise clearly", t_all_fail_raises)

# --- TLS: the checks that actually matter ------------------------------------

print("tls validation")

if _online() and _intercepted():
    print("  WARNING: TLS appears to be intercepted by a proxy that issues its")
    print("  own certificates. The checks below cannot prove anything here.")


def t_correct_host_accepted():
    ip = _probe_ip()
    conn = _PinnedHTTPSConnection(PROBE, ip, context=ssl.create_default_context(), timeout=20)
    conn.request("GET", "/", headers={"User-Agent": "agup"})
    assert conn.getresponse().status in (200, 401, 403)
    conn.close()


def t_mismatched_host_rejected():
    """The hostile-answer case: a valid cert for the wrong name must fail."""
    ip = _probe_ip()
    if _intercepted():
        raise _Skip("TLS intercepted; result would be meaningless")
    conn = _PinnedHTTPSConnection(
        "not-the-real-host.example.com", ip, context=ssl.create_default_context(), timeout=20
    )
    try:
        conn.connect()
    except ssl.SSLCertVerificationError:
        return
    raise AssertionError("mismatched hostname was NOT rejected")


def t_verification_stays_on():
    ip = _probe_ip()
    conn = _PinnedHTTPSConnection(PROBE, ip, context=ssl.create_default_context(), timeout=20)
    conn.connect()
    assert conn.sock.context.check_hostname is True, "check_hostname disabled"
    assert conn.sock.context.verify_mode == ssl.CERT_REQUIRED, "verify_mode weakened"
    assert conn.sock.server_hostname == PROBE, "SNI does not carry the real hostname"
    conn.close()


check("correct hostname accepted over a pinned IP", t_correct_host_accepted)
check("MISMATCHED hostname rejected", t_mismatched_host_rejected)
check("hostname checking and verification remain on", t_verification_stays_on)

# --- routing -----------------------------------------------------------------

print("routing")


def t_system_first():
    import http.client

    queried = []
    orig = R._query_doh
    R._query_doh = lambda *a, **k: queried.append(a) or ["1.2.3.4"]
    R.clear_cache()
    try:
        conn = DoHHTTPSHandler()._build("localhost:443")
        assert type(conn) is http.client.HTTPSConnection
        assert not queried, "DoH used for a name the system could resolve"
    finally:
        R._query_doh = orig
        R.clear_cache()


def t_fallback_announced_once():
    told = []
    orig = R._query_doh
    R._query_doh = lambda e, h, record="A": ["9.9.9.9"] if record == "A" else []
    R.clear_cache()
    try:
        handler = DoHHTTPSHandler(on_fallback=lambda h, a: told.append((h, a)))
        conn = handler._build("nonexistent-agup-test.invalid:443")
        assert isinstance(conn, _PinnedHTTPSConnection)
        handler._build("nonexistent-agup-test.invalid:443")
        assert len(told) == 1, f"announced {len(told)} times, expected once"
    finally:
        R._query_doh = orig
        R.clear_cache()


def t_doh_only_skips_system():
    orig = R._query_doh
    R._query_doh = lambda e, h, record="A": ["1.2.3.4"] if record == "A" else []
    R.clear_cache()
    try:
        conn = DoHHTTPSHandler(always=True)._build("localhost:443")
        assert isinstance(conn, _PinnedHTTPSConnection)
    finally:
        R._query_doh = orig
        R.clear_cache()


check("system resolver used when it works", t_system_first)
check("fallback announced once, not per request", t_fallback_announced_once)
check("--doh-only skips the system resolver", t_doh_only_skips_system)

print()
print(f"FAILURES: {FAILS}" if FAILS else "ALL PASS")
sys.exit(1 if FAILS else 0)
