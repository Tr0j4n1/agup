import os, sys, tempfile
sys.path.insert(0, "src")

from agup.outcome import Outcome, RunReport, Status, EXIT_OK, EXIT_FAILED, EXIT_SKIPPED
from agup.endpoints import Endpoints, ConfigError, validate_endpoint, is_trusted_download
from agup.integrity import PinStore, verify_artifact, IntegrityError, digest_file
from agup.versions import parse_version, compare_versions, is_newer, Release, select_latest

fails = []
def check(name, fn):
    try:
        fn(); print(f"  ok   {name}")
    except Exception as e:
        fails.append(name); print(f"  FAIL {name}: {e}")

print("exit codes")
def t_exit_ok():
    r = RunReport()
    r.add(Outcome.updated("ide","2.5.4","2.5.5")); r.add(Outcome.current("cli","1.1.22"))
    assert r.exit_code() == EXIT_OK, r.exit_code()
def t_exit_skip():
    r = RunReport()
    r.add(Outcome.updated("ide","2.5.4","2.5.5"))
    r.add(Outcome.skipped("hub","process running (PID 78561)"))
    assert r.exit_code() == EXIT_SKIPPED, r.exit_code()
def t_exit_fail_wins():
    r = RunReport()
    r.add(Outcome.skipped("hub","running")); r.add(Outcome.failed("cli","checksum mismatch"))
    assert r.exit_code() == EXIT_FAILED
def t_summary():
    r = RunReport()
    r.add(Outcome.updated("ide","2.5.4","2.5.5")); r.add(Outcome.skipped("hub","running"))
    r.add(Outcome.current("cli","1.1.22"))
    assert "1 updated" in r.summary_line() and "1 skipped" in r.summary_line()
check("clean run exits 0", t_exit_ok)
check("skip-only exits 2 (was 1 upstream)", t_exit_skip)
check("failure outranks skip", t_exit_fail_wins)
check("summary line", t_summary)

print("endpoints")
def t_default():
    e = Endpoints.resolve(env={})
    assert "antigravity-ide-auto-updater-974169037036.us-central1.run.app" in e.ide
    assert e.cli.endswith("/manifests")
def t_env():
    e = Endpoints.resolve(env={"AGUP_IDE_ENDPOINT":"https://mirror.internal/rel"})
    assert e.ide == "https://mirror.internal/rel"
    assert "run.app" in e.hub
def t_precedence():
    e = Endpoints.resolve(overrides={"ide":"https://flag/x"},
        env={"AGUP_IDE_ENDPOINT":"https://env/x"},
        toml_config={"endpoints":{"ide":"https://toml/x"}})
    assert e.ide == "https://flag/x", e.ide
def t_toml_over_default():
    e = Endpoints.resolve(env={}, toml_config={"endpoints":{"hub":"https://toml/h"}})
    assert e.hub == "https://toml/h"
def t_project():
    e = Endpoints.resolve(env={}, project="123", region="eu-west1")
    assert "123.eu-west1.run.app" in e.ide
def t_http_refused():
    try:
        validate_endpoint("http://mirror/x"); assert False, "should refuse"
    except ConfigError: pass
def t_http_allowed():
    assert validate_endpoint("http://mirror/x", allow_insecure=True) == "http://mirror/x"
def t_trusted():
    assert is_trusted_download("https://dl.google.com/a.tar.gz")
    assert is_trusted_download("https://x.storage.googleapis.com/a")
    assert not is_trusted_download("https://evil.com/a")
    assert not is_trusted_download("http://dl.google.com/a")
    assert not is_trusted_download("https://dl.google.com.evil.com/a")
check("defaults match known endpoints", t_default)
check("env override", t_env)
check("flag > env > toml", t_precedence)
check("toml > default", t_toml_over_default)
check("project/region override", t_project)
check("plain http refused", t_http_refused)
check("http allowed when explicit", t_http_allowed)
check("download host allowlist", t_trusted)

print("integrity")
with tempfile.TemporaryDirectory() as d:
    art = os.path.join(d, "a.tar.gz")
    with open(art,"wb") as f: f.write(b"payload-v1")
    art2 = os.path.join(d, "b.tar.gz")
    with open(art2,"wb") as f: f.write(b"payload-TAMPERED")
    real = digest_file(art)
    store = PinStore(os.path.join(d,"pins.json"))

    def t_server_sha256():
        r = verify_artifact(art,"ide","2.5.5",sha256=real,store=store)
        assert r.tier == "server-sha256"
    def t_server_bad():
        try:
            verify_artifact(art,"ide","2.5.5",sha256="0"*64,store=store); assert False
        except IntegrityError: pass
    def t_first_sight():
        r = verify_artifact(art,"hub","2.11.0",store=store)
        assert r.tier == "first-sight", r.tier
        assert store.get("hub","2.11.0") == real
    def t_pinned_ok():
        r = verify_artifact(art,"hub","2.11.0",store=store)
        assert r.tier == "pinned", r.tier
    def t_pin_mismatch():
        try:
            verify_artifact(art2,"hub","2.11.0",store=store); assert False, "tamper not caught"
        except IntegrityError as e: assert "Pin mismatch" in str(e)
    def t_strict():
        try:
            verify_artifact(art,"cli","9.9.9",store=store,strict=True); assert False
        except IntegrityError as e: assert "Strict mode" in str(e)
    def t_forget():
        assert store.forget("hub","2.11.0")
        assert store.get("hub","2.11.0") is None
    def t_perms():
        verify_artifact(art,"ide","3.0.0",store=store)
        assert oct(os.stat(store.path).st_mode)[-3:] == "600"
    check("server sha256 verified", t_server_sha256)
    check("server sha256 mismatch raises", t_server_bad)
    check("first sight records pin", t_first_sight)
    check("second run matches pin", t_pinned_ok)
    check("TAMPERED artifact blocked", t_pin_mismatch)
    check("strict refuses first sight", t_strict)
    check("forget clears pin", t_forget)
    check("store is 0600", t_perms)

print("version detection (VS Code fork)")
import json as _json, tempfile as _tf
def _bundle(**product):
    d=_tf.mkdtemp(); app=os.path.join(d,"resources","app"); os.makedirs(app)
    _json.dump({"version":"1.107.0"}, open(os.path.join(app,"package.json"),"w"))
    if product: _json.dump(product, open(os.path.join(app,"product.json"),"w"))
    return d
from agup.install import read_installed_version, read_product_identity, Paths
def t_idever():
    d=_bundle(version="1.107.0", ideVersion="1.23.2", nameLong="Antigravity")
    assert read_installed_version(d)=="1.23.2", "must prefer ideVersion over VS Code base"
def t_fallback():
    d=_bundle(version="2.5.5", nameLong="Antigravity")
    assert read_installed_version(d)=="2.5.5"
def t_no_product():
    d=_bundle()
    assert read_installed_version(d)=="1.107.0"
def t_missing():
    assert read_installed_version("/nonexistent/xyz")=="0.0.0"
def t_identity():
    d=_bundle(ideVersion="1.23.2", nameLong="Antigravity", applicationName="antigravity")
    assert read_product_identity(d)["nameLong"]=="Antigravity"
def t_override():
    p=Paths.for_scope("user").with_overrides(ide_dir="/usr/share/antigravity")
    assert p.ide_dir=="/usr/share/antigravity"
    assert p.hub_dir.endswith("opt/Antigravity")
check("ideVersion beats VS Code version", t_idever)
check("falls back to product version", t_fallback)
check("falls back to package.json", t_no_product)
check("absent path = 0.0.0", t_missing)
check("product identity read", t_identity)
check("path override applies to one component only", t_override)

print("progress bar")
import io as _io
from agup.fetch import ProgressBar, format_bytes, format_duration
def t_fmt():
    assert format_bytes(512)=="512 B"
    assert format_bytes(1536)=="1.5 KB"
    assert format_duration(95)=="01:35"
    assert format_duration(3725)=="1:02:05"
    assert format_duration(float("inf"))=="--:--"
def t_render():
    b=_io.StringIO(); bar=ProgressBar("  ide",stream=b,force=True,interval=0)
    bar.started -= 1.0  # past warmup so rate/ETA render
    for d in (0, 500, 1000): bar.update(d,1000)
    bar.finish(1000,1000)
    out=b.getvalue()
    assert "50.0%" in out and "100.0%" in out and "ETA" in out
    assert out.endswith("\n")
def t_warmup():
    b=_io.StringIO(); bar=ProgressBar("  ide",stream=b,force=True,interval=0)
    bar.update(500,1000)   # immediately: rate is meaningless
    assert "ETA --:--" in b.getvalue(), b.getvalue()
def t_no_length():
    b=_io.StringIO(); bar=ProgressBar("  hub",stream=b,force=True,interval=0)
    bar.update(5_000_000,0); bar.finish(5_000_000,0)
    assert "MB" in b.getvalue() and "%" not in b.getvalue()
def t_not_tty():
    b=_io.StringIO(); bar=ProgressBar("  cli",stream=b)  # isatty() False
    assert bar.enabled is False
    for d in range(0,1000,100): bar.update(d,1000)
    bar.finish(1000,1000)
    assert b.getvalue()=="", "must stay silent off-terminal"
def t_throttle():
    b=_io.StringIO(); bar=ProgressBar("  x",stream=b,force=True,interval=10.0)
    for d in range(0,100000,1000): bar.update(d,1000000)
    assert b.getvalue().count("\r") <= 2
def t_abort():
    b=_io.StringIO(); bar=ProgressBar("  y",stream=b,force=True,interval=0)
    bar.update(500,1000); bar.abort()
    assert b.getvalue().endswith("\r")
def t_restart():
    b=_io.StringIO(); bar=ProgressBar("  z",stream=b,force=True,interval=0)
    fresh = bar.restart()
    assert fresh.enabled is True and fresh.label == "  z" and fresh is not bar
check("byte and duration formatting", t_fmt)
check("bar renders percent and ETA", t_render)
check("rate suppressed during warmup", t_warmup)
check("no content-length shows bytes only", t_no_length)
check("silent when not a terminal", t_not_tty)
check("redraws are throttled", t_throttle)
check("abort clears the line", t_abort)
check("retry gets a fresh bar", t_restart)

print("versions")
def t_parse():
    assert parse_version("2.5.5") == (2,5,5)
    assert parse_version("1.23.2-1776332190") == (1,23,2,1776332190)
    assert parse_version("") == (0,)
def t_cmp():
    assert compare_versions("2.11.0","2.5.5") == 1
    assert compare_versions("2.5.5","2.5.5") == 0
    assert compare_versions("1.0","1.0.0") == 0
def t_newer():
    assert is_newer("2.5.5","0.0.0")
    assert not is_newer("2.5.5","2.5.5")
    assert is_newer("2.11.0","2.5.5")
def t_release():
    r = Release.from_payload({"version":"2.5.5","url":"https://dl.google.com/a.tar.gz","sha256":"a"*64})
    assert r.sha256 == "a"*64
def t_bad_hex():
    try:
        Release.from_payload({"version":"1","url":"https://x/y","sha256":"zz"}); assert False
    except ValueError: pass
def t_select():
    rels=[Release("2.5.5","https://x/a"),Release("2.11.0","https://x/b"),Release("1.9.9","https://x/c")]
    assert select_latest(rels).version == "2.11.0"
    assert select_latest([]) is None
check("parse tolerates real-world strings", t_parse)
check("numeric compare, not lexical", t_cmp)
check("0.0.0 means not installed", t_newer)
check("release payload parse", t_release)
check("bad hex rejected", t_bad_hex)
check("select latest", t_select)

print()
print(f"{'FAILURES: ' + str(fails) if fails else 'ALL PASS'}")
sys.exit(1 if fails else 0)
