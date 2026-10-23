# -*- coding: utf-8 -*-
# Small loader. At every Revit start it brings this extension up to date from
# the BIM Config "release" folder (the files listed in release\manifest.json,
# plus everything in release\SJ.tab\BIMGuard.panel), then runs lib\vt_guard_main.py, which is the guard itself.
#   release\lib\...                    -> lib\...
#   release\SJ.tab\BIMGuard.panel\...  -> SAA_T5.tab\Template.panel\...
#   release\SJ.tab\<other>\...         -> SAA_T5.tab\<other>\...
# Anything new that the BIM admins publish to the release folder is picked up
# on the next Revit start, with no change to this file.
import os, json, time, traceback

LOG_ENABLED = False          # True writes %LOCALAPPDATA%\vt_guard\vt_guard_log.txt

EXT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP", "."),
                         "vt_guard")
# release paths -> paths in this extension (first match wins)
PATH_MAP = [
    ("SJ.tab/BIMGuard.panel/", "SAA_T5.tab/Template.panel/"),
    ("SJ.tab/", "SAA_T5.tab/"),
]
# release folders copied in full, on top of the manifest list
SCAN_DIRS = ["SJ.tab/BIMGuard.panel"]
SP_LIBRARY = "A&D AR BIM Management - Documents"
SP_FOLDER = "BIM Config"
SP_SHORTCUT = "A&D AR BIM Management - BIM Config"
SHARED_CONFIG_DIR = r"S:\SAA_Temp\Library\33 BIM Library\10 BIM Config"


def _state(name, text):
    try:
        if not os.path.isdir(STATE_DIR):
            os.makedirs(STATE_DIR)
        with open(os.path.join(STATE_DIR, name), "w") as f:
            f.write(text)
    except Exception:
        pass


def _read_state(name):
    try:
        with open(os.path.join(STATE_DIR, name)) as f:
            return f.read().strip()
    except Exception:
        return ""


def find_config_dir():
    """The BIM Config folder that has release\\manifest.json. The SharePoint
    folder (through OneDrive) comes before the S: drive, which can be stale."""
    cands = [os.environ.get("VT_CONFIG_DIR", "")]
    roots = []
    for name in ("OneDriveCommercial", "OneDrive"):
        r = os.environ.get(name, "")
        if r and r not in roots:
            roots.append(r)
    prof = os.environ.get("USERPROFILE", "")
    try:
        for n in sorted(os.listdir(prof)):
            p = os.path.join(prof, n)
            if n.lower().startswith("onedrive - ") and p not in roots:
                roots.append(p)
    except Exception:
        pass
    for r in roots:
        cands.append(os.path.join(r, "Shortcuts", SP_SHORTCUT))
        cands.append(os.path.join(r, "Shortcuts", SP_LIBRARY, SP_FOLDER))
        cands.append(os.path.join(r, SP_LIBRARY, SP_FOLDER))
    cands.append(SHARED_CONFIG_DIR)
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "release", "manifest.json")):
            return c
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "vt_rules.json")):
            return c
    return ""


def local_path(rel):
    rel = rel.replace("\\", "/").lstrip("/")
    for src, dst in PATH_MAP:
        if rel.startswith(src):
            rel = dst + rel[len(src):]
            break
    return os.path.join(EXT_DIR, *rel.split("/"))


def _same(a, b):
    if not os.path.isfile(b) or os.path.getsize(a) != os.path.getsize(b):
        return False
    with open(a, "rb") as fa:
        with open(b, "rb") as fb:
            return fa.read() == fb.read()


def sync_release(config_dir):
    """Copy every changed manifest file. Returns (version, copied, failed)."""
    rel_dir = os.path.join(config_dir, "release")
    with open(os.path.join(rel_dir, "manifest.json")) as f:
        manifest = json.load(f)
    version = manifest.get("version", "?")
    files = list(manifest.get("files", []))
    # also everything in the release button folders, in case a new button was
    # dropped in without being added to the manifest
    seen = set(r.replace("\\", "/").lower() for r in files)
    for top in SCAN_DIRS:
        for root, dirs, names in os.walk(os.path.join(rel_dir, *top.split("/"))):
            for n in names:
                if n.lower().endswith((".pyc", ".new", ".tmp")):
                    continue
                rel = os.path.relpath(os.path.join(root, n), rel_dir).replace("\\", "/")
                if rel.lower() not in seen:
                    seen.add(rel.lower())
                    files.append(rel)
    copied, failed = [], []
    for rel in files:
        src = os.path.join(rel_dir, *rel.replace("\\", "/").split("/"))
        dst = local_path(rel)
        try:
            if not os.path.isfile(src):
                failed.append(rel + " (missing in release - still syncing?)")
                continue
            if _same(src, dst):
                continue
            folder = os.path.dirname(dst)
            if not os.path.isdir(folder):
                os.makedirs(folder)
            tmp = dst + ".new"
            with open(src, "rb") as fs:
                data = fs.read()
            with open(tmp, "wb") as ft:
                ft.write(data)
            if os.path.exists(dst):
                os.remove(dst)
            os.rename(tmp, dst)
            copied.append(rel)
        except Exception as e:
            failed.append("{} ({})".format(rel, e))
    return version, copied, failed


CONFIG_DIR = find_config_dir()
if CONFIG_DIR and not os.environ.get("VT_CONFIG_DIR"):
    # lib\vt_lock.py does not know the "shortcut made on the folder itself"
    # layout and would fall back to the S: drive copy of the rules.
    os.environ["VT_CONFIG_DIR"] = CONFIG_DIR

stamp = time.strftime("%Y-%m-%d %H:%M:%S")
if not CONFIG_DIR:
    _state("sync_status.txt", "{} - BIM Config folder not found, using the files "
                              "already in this extension".format(stamp))
else:
    try:
        version, copied, failed = sync_release(CONFIG_DIR)
        msg = "{} - release {} from {}: {} file(s) updated".format(
            stamp, version, CONFIG_DIR, len(copied))
        if failed:
            msg += ", {} FAILED: {}".format(len(failed), "; ".join(failed))
        _state("sync_status.txt", msg)
        if not failed:
            _state("release_version.txt", version)
        if copied:
            _state("sync_last_copied.txt", "\n".join(copied))
    except Exception:
        _state("sync_status.txt", "{} - update failed: {}".format(
            stamp, traceback.format_exc()))

_main = os.path.join(EXT_DIR, "lib", "vt_guard_main.py")
if os.path.isfile(_main):
    _g = dict(globals())
    _g["__file__"] = _main
    _g["LOG_ENABLED"] = LOG_ENABLED
    with open(_main) as _f:
        _code = compile(_f.read(), _main, "exec")
    try:
        exec(_code, _g)
        _state("guard_status.txt", "{} - started, release {}".format(
            stamp, _read_state("release_version.txt") or "?"))
    except Exception:
        _state("guard_status.txt", "{} - FAILED: {}".format(
            stamp, traceback.format_exc()))
        raise
else:
    _state("guard_status.txt", "{} - lib\\vt_guard_main.py missing".format(stamp))
