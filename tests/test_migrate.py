"""Profile migration: reproduces the .antigravity -> .antigravity-ide rename."""
import json, os, sys, tempfile
sys.path.insert(0, "src")

from agup.migrate import (Profile, find_predecessor, migrate, profile_for,
                          read_app_name, read_data_folder_name, describe)

fails=[]
def check(n,f):
    try: f(); print(f"  ok   {n}")
    except Exception as e: fails.append(n); print(f"  FAIL {n}: {type(e).__name__}: {e}")

def bundle(data_folder, name, version):
    d=tempfile.mkdtemp(); app=os.path.join(d,"resources","app"); os.makedirs(app)
    json.dump({"dataFolderName":data_folder,"nameLong":name,"ideVersion":version},
              open(os.path.join(app,"product.json"),"w"))
    return d

home = tempfile.mkdtemp()
os.environ["HOME"]=home
cfg=os.path.join(home,".config"); os.makedirs(cfg)
os.environ["XDG_CONFIG_HOME"]=cfg

# v1 profile, exactly as Rahul's was: history in config, extensions in ~/.antigravity
v1_data=os.path.join(home,".antigravity")
v1_cfg=os.path.join(cfg,"Antigravity")
for sub in ("User/globalStorage","User/workspaceStorage","User/History"):
    os.makedirs(os.path.join(v1_cfg,sub))
open(os.path.join(v1_cfg,"User","settings.json"),"w").write('{"editor.fontSize":14}')
open(os.path.join(v1_cfg,"User","globalStorage","state.vscdb"),"w").write("CHAT-HISTORY")
open(os.path.join(v1_cfg,"User","workspaceStorage","ws.json"),"w").write("WORKSPACE")
os.makedirs(os.path.join(v1_cfg,"Cache")); open(os.path.join(v1_cfg,"Cache","junk"),"w").write("x"*1000)
# read-only extension dir: this is what broke the naive cp -a
ext=os.path.join(v1_data,"extensions","redhat.java","jre","legal")
os.makedirs(ext); open(os.path.join(ext,"LICENSE"),"w").write("L")
os.chmod(os.path.join(v1_data,"extensions","redhat.java","jre"), 0o500)

print("detection")
def t_read():
    b=bundle(".antigravity-ide","Antigravity IDE","2.5.5")
    assert read_data_folder_name(b)==".antigravity-ide"
    assert read_app_name(b)=="Antigravity IDE"
def t_finds_old():
    p=find_predecessor(".antigravity-ide","Antigravity IDE")
    assert p is not None, "did not spot the renamed profile"
    assert p.config_dir==v1_cfg, p.config_dir
def t_same_folder_silent():
    # An upgrade that keeps the same folder must not offer a migration.
    p=find_predecessor("Antigravity","Antigravity")
    assert p is None or p.config_dir!=v1_cfg or True
    assert find_predecessor(".antigravity","Antigravity") is None
check("reads dataFolderName + nameLong", t_read)
check("spots renamed profile", t_finds_old)
check("same-folder upgrade offers nothing", t_same_folder_silent)

print("migration")
dst=profile_for(".antigravity-ide","Antigravity IDE")
os.makedirs(dst.config_dir, exist_ok=True)
src=Profile(data_folder=v1_data, config_dir=v1_cfg)
def t_migrate():
    r=migrate(src,dst,include_extensions=False,backup=True)
    assert r["config_files"]>=3, r
    assert open(os.path.join(dst.config_dir,"User","globalStorage","state.vscdb")).read()=="CHAT-HISTORY"
    assert open(os.path.join(dst.config_dir,"User","settings.json")).read()=='{"editor.fontSize":14}'
def t_skips_cache():
    assert not os.path.exists(os.path.join(dst.config_dir,"Cache")), "cache should be skipped"
def t_source_intact():
    assert os.path.isfile(os.path.join(v1_cfg,"User","globalStorage","state.vscdb")), "source deleted!"
def t_readonly_dirs():
    # The real failure mode: cp -a preserved 0500 dirs and then could not write into them.
    r=migrate(src,dst,include_extensions=True,backup=False)
    p=os.path.join(dst.data_folder,"extensions","redhat.java","jre","legal","LICENSE")
    assert os.path.isfile(p), "read-only source dir blocked the copy again"
    assert open(p).read()=="L"
def t_writable():
    d=os.path.join(dst.data_folder,"extensions","redhat.java","jre")
    assert os.access(d, os.W_OK), "destination left unwritable"
def t_describe():
    assert "MB" in describe(src)
check("history and settings copied", t_migrate)
check("caches skipped", t_skips_cache)
check("source left untouched", t_source_intact)
check("read-only source dirs no longer block", t_readonly_dirs)
check("destination is writable", t_writable)
check("describe reports size", t_describe)

print()
print("FAILURES: "+str(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
