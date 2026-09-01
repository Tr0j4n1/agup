"""End-to-end: mock release server, real download/verify/extract/install path."""
import hashlib, http.server, json, os, sys, tarfile, tempfile, threading, io
sys.path.insert(0, "src")

from agup.endpoints import Endpoints
from agup.integrity import PinStore
from agup.outcome import Status, EXIT_OK, EXIT_FAILED, EXIT_SKIPPED
from agup.update import Options, update_bundle
from agup import fetch

work = tempfile.mkdtemp()

def make_bundle(version, marker=b"real"):
    """Build a tar.gz that looks like an Antigravity IDE bundle."""
    path = os.path.join(work, f"ide-{version}-{marker.decode()}.tar.gz")
    with tarfile.open(path, "w:gz") as tar:
        pkg = json.dumps({"version": version}).encode()
        for name, data in [
            ("Antigravity IDE/resources/app/package.json", pkg),
            ("Antigravity IDE/antigravity-ide", b"#!/bin/sh\necho " + marker),
        ]:
            info = tarfile.TarInfo(name); info.size = len(data); info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return path

BUNDLES = {}
STATE = {"version": "2.5.5", "send_sha": True, "serve": None}

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/releases"):
            v = STATE["version"]
            if STATE.get("real_shape"):
                # Exactly what the live IDE/Hub feeds return: no URL at all.
                body = {"version": v, "execution_id": "4923483625488384"}
            else:
                body = {"version": v, "url": f"http://127.0.0.1:{PORT}/dl/{v}.tar.gz"}
            if STATE["send_sha"]:
                body["sha256"] = hashlib.sha256(open(BUNDLES[v],"rb").read()).hexdigest()
            payload = json.dumps([body]).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(payload))); self.end_headers()
            self.wfile.write(payload)
        elif self.path.startswith("/dl/"):
            src = STATE["serve"] or BUNDLES[STATE["version"]]
            data = open(src,"rb").read()
            self.send_response(200); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        else:
            self.send_response(404); self.end_headers()

BUNDLES["2.5.5"] = make_bundle("2.5.5")
BUNDLES["2.6.0"] = make_bundle("2.6.0")
TAMPERED = make_bundle("2.6.0", b"evil")

srv = http.server.HTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# Local mock server: allow http and bypass the Google host allowlist.
_orig = fetch.download
def patched(url, dest, **kw):
    kw["enforce_trusted_host"] = False
    return _orig(url, dest, **kw)
fetch.download = patched
import agup.update as U
U.download = patched

fails = []
def check(name, fn):
    try: fn(); print(f"  ok   {name}")
    except Exception as e:
        fails.append(name); print(f"  FAIL {name}: {type(e).__name__}: {e}")

home = os.path.join(work, "home"); os.makedirs(home)
os.environ["HOME"] = home
store = PinStore(os.path.join(work, "pins.json"))
eps = Endpoints.resolve(env={}, overrides={"ide": f"http://127.0.0.1:{PORT}/releases"},
                        allow_insecure=True)
quiet = lambda t: None
target = os.path.join(home, "opt", "Antigravity-IDE")

print("end-to-end install")
def t_fresh():
    o = update_bundle("ide", eps, Options(pin_store=store), quiet)
    assert o.status is Status.UPDATED, o.detail
    assert o.from_version == "0.0.0" and o.to_version == "2.5.5"
    assert os.path.exists(os.path.join(target, "antigravity-ide"))
def t_idempotent():
    o = update_bundle("ide", eps, Options(pin_store=store), quiet)
    assert o.status is Status.CURRENT, o.detail
def t_upgrade():
    STATE["version"] = "2.6.0"
    o = update_bundle("ide", eps, Options(pin_store=store), quiet)
    assert o.status is Status.UPDATED and o.to_version == "2.6.0", o.detail
    assert b"real" in open(os.path.join(target,"antigravity-ide"),"rb").read()
def t_dry_run():
    STATE["version"] = "2.7.0"; BUNDLES["2.7.0"] = make_bundle("2.7.0")
    o = update_bundle("ide", eps, Options(pin_store=store, dry_run=True), quiet)
    assert o.status is Status.SKIPPED, o.detail
    assert "2.7.0" in o.detail
    assert "2.6.0" == json.load(open(os.path.join(target,"resources/app/package.json")))["version"]
check("fresh install from nothing", t_fresh)
check("second run is a no-op", t_idempotent)
check("upgrade replaces bundle", t_upgrade)
check("dry run installs nothing", t_dry_run)

print("integrity end-to-end")
def t_bad_checksum():
    STATE["version"]="2.8.0"; BUNDLES["2.8.0"]=make_bundle("2.8.0"); STATE["serve"]=TAMPERED
    o = update_bundle("ide", eps, Options(pin_store=store), quiet)
    STATE["serve"]=None
    assert o.status is Status.FAILED, o.detail
    assert "mismatch" in o.detail
def t_tofu_then_tamper():
    STATE["send_sha"]=False; STATE["version"]="3.0.0"; BUNDLES["3.0.0"]=make_bundle("3.0.0")
    o = update_bundle("ide", eps, Options(pin_store=store), quiet)
    assert o.status is Status.UPDATED, o.detail
    assert store.get("ide","3.0.0") is not None
    # same version, different artifact -> must fail
    STATE["serve"]=make_bundle("3.0.0", b"evil")
    o2 = update_bundle("ide", eps, Options(pin_store=store, force=True), quiet)
    STATE["serve"]=None; STATE["send_sha"]=True
    assert o2.status is Status.FAILED, o2.detail
    assert "Pin mismatch" in o2.detail
def t_strict():
    STATE["send_sha"]=False; STATE["version"]="4.0.0"; BUNDLES["4.0.0"]=make_bundle("4.0.0")
    o = update_bundle("ide", eps, Options(pin_store=store, strict=True), quiet)
    STATE["send_sha"]=True
    assert o.status is Status.FAILED and "Strict" in o.detail, o.detail
def t_untrusted_host():
    o = update_bundle("ide", Endpoints.resolve(env={},
        overrides={"ide": f"http://127.0.0.1:{PORT}/releases"}, allow_insecure=True),
        Options(pin_store=store), quiet)
    # sanity: patched download bypasses host check; verify the real one blocks
    try:
        _orig("https://evil.example/x.tar.gz", "/tmp/x")
        assert False, "untrusted host not blocked"
    except fetch.UntrustedDownloadError: pass
check("server checksum mismatch blocks install", t_bad_checksum)
check("TOFU pin catches swapped artifact", t_tofu_then_tamper)
check("strict mode refuses first sight", t_strict)
check("untrusted download host refused", t_untrusted_host)

print("failure modes")
def t_dead_endpoint():
    dead = Endpoints.resolve(env={}, overrides={"ide":"http://127.0.0.1:1/releases"}, allow_insecure=True)
    o = update_bundle("ide", dead, Options(pin_store=store), quiet)
    assert o.status is Status.FAILED and "Cannot reach" in o.detail, o.detail
def t_unwritable():
    # Root can write anywhere, so exercise the branch directly rather than
    # relying on filesystem permissions.
    import agup.update as U
    orig = U.is_writable
    U.is_writable = lambda p: False
    try:
        STATE["version"]="7.0.0"; BUNDLES["7.0.0"]=make_bundle("7.0.0")
        o = update_bundle("ide", eps, Options(pin_store=store), quiet)
    finally:
        U.is_writable = orig
    assert o.status is Status.SKIPPED and "not writable" in o.detail, o.detail
def t_running_process_skips():
    import agup.update as U
    orig = U.running_pids
    U.running_pids = lambda *a: ["78561","78574"]
    try:
        STATE["version"]="6.0.0"; BUNDLES["6.0.0"]=make_bundle("6.0.0")
        o = update_bundle("ide", eps, Options(pin_store=store), quiet)
        o2 = update_bundle("ide", eps, Options(pin_store=store, force=True), quiet)
    finally:
        U.running_pids = orig
    assert o.status is Status.SKIPPED and "78561" in o.detail, o.detail
    assert o2.status is Status.UPDATED, o2.detail
def t_self_not_matched():
    from agup.install import running_pids
    # Our own process tree mentions these strings; must not self-match.
    assert running_pids("Antigravity-IDE", "antigravity-ide") == []
check("unreachable endpoint = FAILED", t_dead_endpoint)
check("running process = SKIPPED, --force overrides", t_running_process_skips)
check("does not match its own process tree", t_self_not_matched)
check("unwritable target = SKIPPED not FAILED", t_unwritable)

print("live feed shape")
def t_real_shape():
    from agup.versions import Release
    from agup.endpoints import Endpoints as E
    r = Release.from_payload({"version":"2.5.5","execution_id":"4923483625488384"})
    assert r.download_url is None and r.execution_id == "4923483625488384"
    e = E.resolve(env={})
    u = e.artifact_url("ide", r.version, r.execution_id)
    assert u.startswith("https://edgedl.me.gvt1.com/") and "2.5.5-4923483625488384" in u, u
    from agup.endpoints import is_trusted_download
    assert is_trusted_download(u)
    assert is_trusted_download(e.artifact_url("hub","2.11.0","999"))
def t_no_url_no_execid():
    from agup.versions import Release
    try:
        Release.from_payload({"version":"1.0.0"}); assert False, "should reject"
    except ValueError: pass
def t_artifact_override():
    from agup.endpoints import Endpoints as E
    e = E.resolve(env={"AGUP_IDE_ARTIFACT":"https://m.internal/{version}-{exec_id}.tar.gz"})
    assert e.artifact_url("ide","1.2.3","abc") == "https://m.internal/1.2.3-abc.tar.gz"
def t_end_to_end_real_shape():
    STATE["real_shape"]=True; STATE["send_sha"]=False
    STATE["version"]="8.0.0"; BUNDLES["8.0.0"]=make_bundle("8.0.0")
    eps2 = Endpoints.resolve(env={}, overrides={"ide": f"http://127.0.0.1:{PORT}/releases",
        "ide_artifact": f"http://127.0.0.1:{PORT}/dl/{{version}}.tar.gz"}, allow_insecure=True)
    o = update_bundle("ide", eps2, Options(pin_store=store), quiet)
    STATE["real_shape"]=False; STATE["send_sha"]=True
    assert o.status is Status.UPDATED and o.to_version=="8.0.0", o.detail
check("parses version+execution_id feed", t_real_shape)
check("rejects release with neither", t_no_url_no_execid)
check("artifact template overridable", t_artifact_override)
check("full install from real-shape feed", t_end_to_end_real_shape)

srv.shutdown()
print()
print("FAILURES: "+str(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
