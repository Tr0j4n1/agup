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
import json as _json, tempfile as _tf, tempfile as _t2, os as _o2
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
from agup.install import write_receipt, read_receipt
def t_receipt_hub():
    # The Hub is a plain Electron app: no package.json, no product.json, and
    # app-update.yml carries only updater config. Without a receipt its
    # version reads 0.0.0 forever and every run re-downloads 163 MB.
    d=_t2.mkdtemp(); _o2.makedirs(_o2.path.join(d,"resources"))
    open(_o2.path.join(d,"resources","app-update.yml"),"w").write("provider: generic\n")
    assert read_installed_version(d)=="0.0.0"
    assert write_receipt(d,"hub","2.11.0")
    assert read_installed_version(d)=="2.11.0"
def t_receipt_is_last():
    # A stale receipt must never override what the bundle itself states.
    d=_bundle(version="1.107.0", ideVersion="2.5.5")
    write_receipt(d,"ide","0.0.1")
    assert read_installed_version(d)=="2.5.5"
def t_receipt_roundtrip():
    d=_t2.mkdtemp(); write_receipt(d,"hub","2.11.0",sha256="ab"*32)
    r=read_receipt(d)
    assert r["component"]=="hub" and r["version"]=="2.11.0"
    assert read_receipt(_t2.mkdtemp()) is None
def t_receipt_corrupt():
    from agup.install import RECEIPT_NAME
    d=_t2.mkdtemp(); open(_o2.path.join(d,RECEIPT_NAME),"w").write("{not json")
    assert read_receipt(d) is None
    assert read_installed_version(d)=="0.0.0"
check("receipt fixes Hub 0.0.0 reinstall loop", t_receipt_hub)
check("bundle metadata beats stale receipt", t_receipt_is_last)
check("receipt round-trips", t_receipt_roundtrip)
check("corrupt receipt ignored", t_receipt_corrupt)
def t_cli_binary_names():
    from agup.update import _find_cli_binary
    # The real archive contains a single file named "antigravity", not "agy".
    for name in ("antigravity","agy","antigravity-cli"):
        d=_t2.mkdtemp(); p=_o2.path.join(d,name)
        open(p,"w").write("#!/bin/sh"); _o2.chmod(p,0o755)
        assert _find_cli_binary(d)==p, name
def t_cli_unknown_name():
    from agup.update import _find_cli_binary
    # Unknown name, single executable: take it rather than failing.
    d=_t2.mkdtemp(); p=_o2.path.join(d,"ag-future-name")
    open(p,"w").write("#!/bin/sh"); _o2.chmod(p,0o755)
    assert _find_cli_binary(d)==p
def t_cli_ambiguous():
    from agup.update import _find_cli_binary
    d=_t2.mkdtemp()
    for n in ("one","two"):
        p=_o2.path.join(d,n); open(p,"w").write("x"); _o2.chmod(p,0o755)
    assert _find_cli_binary(d) is None, "ambiguous archive must not guess"
check("CLI binary found under any known name", t_cli_binary_names)
check("unknown name, lone executable taken", t_cli_unknown_name)
check("ambiguous archive refuses to guess", t_cli_ambiguous)

print("desktop entries")
from agup.desktop import (render_entry, install_desktop_entry, remove_desktop_entry,
                          find_icon, DesktopTarget)
import struct as _st, zlib as _zl
def _png(w,h):
    ihdr=_st.pack(">II",w,h)+b"\x08\x06\x00\x00\x00"; c=b"IHDR"+ihdr
    return b"\x89PNG\r\n\x1a\n"+_st.pack(">I",13)+c+_st.pack(">I",_zl.crc32(c))
def _mkbundle(binname="antigravity-ide", icon=True, size=512):
    d=_t2.mkdtemp()
    b=_o2.path.join(d,binname); open(b,"w").write("#!/bin/sh\n"); _o2.chmod(b,0o755)
    if icon:
        p=_o2.path.join(d,"resources","app","resources","linux"); _o2.makedirs(p)
        open(_o2.path.join(p,"code.png"),"wb").write(_png(size,size))
    return d, b
def t_render():
    e = render_entry(name="Antigravity IDE", executable="/x/agy-ide",
                     icon="antigravity-ide", comment="c", wm_class="Antigravity")
    assert e.startswith("[Desktop Entry]")
    assert "Exec=/x/agy-ide %F" in e
    assert "[Desktop Action new-window]" in e
    assert "--new-window" in e
    assert e.endswith("\n")
def t_icon_found():
    d,_ = _mkbundle()
    assert find_icon(d).endswith("code.png")
def t_icon_absent():
    d,_ = _mkbundle(icon=False)
    assert find_icon(d) is None
def t_install(tmpenv={}):
    d,b = _mkbundle()
    home=_t2.mkdtemp(); old=_o2.environ.get("XDG_DATA_HOME")
    _o2.environ["XDG_DATA_HOME"]=home
    try:
        path = install_desktop_entry("ide", d, b, scope="user")
        assert path and _o2.path.isfile(path), path
        body=open(path).read()
        assert f"Exec={b} %F" in body
        assert _o2.path.isfile(_o2.path.join(home,"icons","hicolor","512x512","apps","antigravity-ide.png"))
        assert "Icon=antigravity-ide\n" in body
        assert remove_desktop_entry("ide", scope="user")
        assert not _o2.path.exists(path)
    finally:
        if old is None: _o2.environ.pop("XDG_DATA_HOME",None)
        else: _o2.environ["XDG_DATA_HOME"]=old
def t_scope():
    t = DesktopTarget.for_scope("system")
    assert t.applications_dir.startswith("/usr/local/")
def t_find_exe():
    from agup.update import _find_executable
    d,b = _mkbundle("antigravity")          # Debian-style name
    assert _find_executable(d,"ide") == b   # still found for the IDE
    d2,b2 = _mkbundle("antigravity-ide")
    assert _find_executable(d2,"ide") == b2
    assert _find_executable(_t2.mkdtemp(),"ide") is None
check("entry body well formed", t_render)
check("icon located in bundle", t_icon_found)
check("missing icon tolerated", t_icon_absent)
check("entry + icon installed and removable", t_install)
def t_icon_1024():
    # A 1024px icon must land in 1024x1024, not 512x512 -- mismatched
    # dimensions make the theme lookup silently fail.
    d,b = _mkbundle(size=1024)
    home=_t2.mkdtemp(); old=_o2.environ.get("XDG_DATA_HOME"); _o2.environ["XDG_DATA_HOME"]=home
    try:
        path = install_desktop_entry("ide", d, b, scope="user")
        assert _o2.path.isfile(_o2.path.join(home,"icons","hicolor","1024x1024","apps","antigravity-ide.png"))
        assert not _o2.path.exists(_o2.path.join(home,"icons","hicolor","512x512","apps","antigravity-ide.png"))
    finally:
        if old is None: _o2.environ.pop("XDG_DATA_HOME",None)
        else: _o2.environ["XDG_DATA_HOME"]=old
def t_icon_odd_size():
    # Non-standard size falls back to an absolute path in Icon=
    d,b = _mkbundle(size=900)
    home=_t2.mkdtemp(); old=_o2.environ.get("XDG_DATA_HOME"); _o2.environ["XDG_DATA_HOME"]=home
    try:
        path = install_desktop_entry("ide", d, b, scope="user")
        body=open(path).read()
        icon_line=[l for l in body.splitlines() if l.startswith("Icon=")][0]
        assert icon_line.startswith("Icon=/"), icon_line
        assert _o2.path.isfile(icon_line.split("=",1)[1])
    finally:
        if old is None: _o2.environ.pop("XDG_DATA_HOME",None)
        else: _o2.environ["XDG_DATA_HOME"]=old
def t_png_size():
    from agup.desktop import read_png_size
    d=_t2.mkdtemp(); p=_o2.path.join(d,"x.png"); open(p,"wb").write(_png(256,256))
    assert read_png_size(p)==(256,256)
    q=_o2.path.join(d,"bad.png"); open(q,"wb").write(b"not a png")
    assert read_png_size(q) is None
check("1024px icon goes to 1024x1024 dir", t_icon_1024)
check("odd size falls back to absolute path", t_icon_odd_size)
check("PNG header parsed, junk rejected", t_png_size)
check("system scope uses /usr/local", t_scope)
check("launcher found under either binary name", t_find_exe)

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
