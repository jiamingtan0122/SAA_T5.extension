# -*- coding: utf-8 -*-
"""Make sure every installed Revit version has
DisableRoomAreaSpaceAutomaticLeader=1 under [Misc] in Revit.ini, so Revit
stops adding leaders to room / area / space tags automatically.
Called from startup.py. Revit reads Revit.ini at launch, so a newly added
line takes effect from the next Revit start."""
import io
import os
import re
import glob
import shutil

KEY = "DisableRoomAreaSpaceAutomaticLeader"
VALUE = "1"


def _patch(path):
    """Returns True if the file was changed."""
    with io.open(path, "r", encoding="utf-16", newline="") as f:
        text = f.read()

    nl = "\r\n" if "\r\n" in text else "\n"
    key_re = re.compile(r"^[ \t]*" + KEY + r"[ \t]*=[ \t]*([^\r\n]*?)[ \t]*\r?$",
                        re.MULTILINE | re.IGNORECASE)
    m = key_re.search(text)
    if m:
        if m.group(1) == VALUE:
            return False
        new = text[:m.start()] + KEY + "=" + VALUE + text[m.end():]
    else:
        misc = re.search(r"^\[Misc\][ \t]*\r?\n", text, re.MULTILINE)
        if misc:
            new = text[:misc.end()] + KEY + "=" + VALUE + nl + text[misc.end():]
        else:
            if text and not text.endswith(("\n", "\r")):
                text += nl
            new = text + "[Misc]" + nl + KEY + "=" + VALUE + nl

    backup = path + ".saa_bak"
    if not os.path.exists(backup):
        shutil.copy2(path, backup)
    with io.open(path, "w", encoding="utf-16", newline="") as f:
        f.write(new)
    return True


def ensure_room_tag_leader_disabled(log=None):
    pattern = os.path.join(os.environ.get("APPDATA", ""),
                           "Autodesk", "Revit", "Autodesk Revit *", "Revit.ini")
    for path in glob.glob(pattern):
        try:
            if _patch(path) and log:
                log("ini_fix: added {}={} to {}".format(KEY, VALUE, path))
        except Exception as ex:
            if log:
                log("ini_fix: failed on {}: {}".format(path, ex))
