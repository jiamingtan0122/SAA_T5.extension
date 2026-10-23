# -*- coding: utf-8 -*-
"""Update the SAA_T5 extension from GitHub, then reload pyRevit.
The BIMGuard Template panel is not on GitHub: it comes from the BIM Config
OneDrive shortcut and is refreshed by startup.py when pyRevit reloads."""
__title__ = "Update\nSAA_T5"
__context__ = "zero-doc"

import os
import clr
clr.AddReference("System")      # Process lives in System.dll
from System.Diagnostics import Process, ProcessStartInfo
from pyrevit import forms

REPO_URL = "https://github.com/jiamingtan0122/SAA_T5.extension.git"
BRANCH = "master"


def find_ext_root():
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(6):
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def find_git():
    for p in os.environ.get("PATH", "").split(os.pathsep):
        exe = os.path.join(p.strip('"'), "git.exe")
        if os.path.isfile(exe):
            return exe
    for base in (os.environ.get("ProgramFiles", ""), os.environ.get("ProgramW6432", ""),
                 os.environ.get("ProgramFiles(x86)", ""),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        exe = os.path.join(base, "Git", "cmd", "git.exe")
        if base and os.path.isfile(exe):
            return exe
    return None


def git(*args):
    """(exit code, output) of git run in the extension folder."""
    psi = ProcessStartInfo(GIT, " ".join('"{}"'.format(a) for a in ("-C", ROOT) + args))
    psi.UseShellExecute = False
    psi.CreateNoWindow = True
    psi.RedirectStandardOutput = True
    psi.RedirectStandardError = True
    p = Process.Start(psi)
    out = p.StandardOutput.ReadToEnd() + p.StandardError.ReadToEnd()
    p.WaitForExit()
    return p.ExitCode, out.strip()


ROOT = find_ext_root()
GIT = find_git()
if not ROOT:
    forms.alert("This SAA_T5 folder was not installed from GitHub.\n"
                "Run 'Install SAA_T5.bat' from the SAA_T5 Installer folder once.",
                title="Update SAA_T5", exitscript=True)
if not GIT:
    forms.alert("Git is not installed on this PC.\n"
                "Run 'Install SAA_T5.bat' from the SAA_T5 Installer folder once.",
                title="Update SAA_T5", exitscript=True)

git("remote", "set-url", "origin", REPO_URL)
UNPUSHED = git("rev-list", "HEAD", "--not", "--remotes")[1]
code, out = git("fetch", "origin", BRANCH)
if code != 0:
    forms.alert("Could not reach GitHub:\n\n" + out, title="Update SAA_T5", exitscript=True)

local = git("rev-parse", "HEAD")[1]
remote = git("rev-parse", "origin/" + BRANCH)[1]
if local == remote:
    forms.alert("SAA_T5 is already up to date.", title="Update SAA_T5", exitscript=True)

# commits made on this PC and not on GitHub would be lost by the update
# (checked against what GitHub had before this fetch, so a history rewrite on
# GitHub does not count as local work)
if UNPUSHED:
    if not forms.alert("This PC has SAA_T5 commits that are not on GitHub. "
                       "Updating will discard them.\n\nUpdate anyway?",
                       title="Update SAA_T5", yes=True, no=True):
        forms.alert("Update cancelled. Nothing was changed.", title="Update SAA_T5",
                    exitscript=True)

# local edits to tracked files would be lost by the update
changed = git("status", "--porcelain", "--untracked-files=no")[1]
if changed:
    if not forms.alert("These files were changed on this PC and will be replaced "
                       "by the GitHub version:\n\n" + changed + "\n\nUpdate anyway?",
                       title="Update SAA_T5", yes=True, no=True):
        forms.alert("Update cancelled. Nothing was changed.", title="Update SAA_T5",
                    exitscript=True)

code, out = git("reset", "--hard", "origin/" + BRANCH)
if code != 0:
    forms.alert("Update failed:\n\n" + out, title="Update SAA_T5", exitscript=True)

log = git("log", "-n", "10", "--format=- %s", "{}..{}".format(local, remote))[1]
msg = "SAA_T5 updated.\n\n" + (log or out)
if forms.alert(msg + "\n\nReload pyRevit now to use it?", title="Update SAA_T5",
               yes=True, no=True):
    from pyrevit.loader import sessionmgr
    sessionmgr.reload_pyrevit()
