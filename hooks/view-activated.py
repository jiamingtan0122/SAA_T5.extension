from System.Collections.Generic import *
from Autodesk.Revit.DB import *
from pyrevit import revit, DB, UI
from pyrevit import script
from pyrevit import forms
from pyrevit import EXEC_PARAMS
from pyrevit.userconfig import user_config
import time
import os

doc = __eventargs__.Document
pview = __eventargs__.PreviousActiveView
if pview:
    pview = pview.Document
cview = __eventargs__.CurrentActiveView.Document
title = doc.Title

# data file will be saved in pyRevit Cache folder per document title.
# Hash of the full path name to handle duplicate files in different folders.
savestatus_datafile = os.path.join(
    os.path.expandvars("%userprofile%\\AppData\\Roaming\\pyRevit\\Cache"),
    title + str(hash(doc.PathName)) + "_savestatus.txt",
)

"""
test if doc has a local file or return
test if save log file exists or write log
test if file is empty or write log
check time
save file and update time or return

"""

# Does not save detached, family, or unsaved documents
active = not doc.IsDetached and not doc.IsFamilyDocument and len(doc.PathName) > 0


def read():
    with open(savestatus_datafile, "r") as f:
        for line in f:
            x = line.split("|")
            return x


def write():
    with open(savestatus_datafile, "w") as f:
        f.write(str(doc.Title) + "|" + str(time.time()))


try:
    if user_config.autosave.get_option("enabled"):
        if active:
            # test if file exists and contains data
            if (
                os.path.exists(savestatus_datafile)
                and os.stat(savestatus_datafile).st_size > 0
            ):
                x = read()
                if str(title) == x[0] and time.time() - float(
                    x[1]
                ) >= user_config.autosave.get_option("interval") and (pview == cview):
                    try:
                        with forms.ProgressBar(
                            title="Autosaving...", indeterminate=True, cancellable=False
                        ) as pb:
                            pb.update_progress(1, 1)
                            doc.Save()
                        write()
                    except:
                        pass
            # if no file exists or file is empty, write current timestamp but do not save file
            else:
                write()
except:
    pass
