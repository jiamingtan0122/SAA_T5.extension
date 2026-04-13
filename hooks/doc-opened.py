# show internal notes when document is opened
##### IMPORTS
from System.Collections.Generic import *
from Autodesk.Revit.DB import *
from pyrevit import revit, DB, UI
from pyrevit import script
from pyrevit import forms
from pyrevit import EXEC_PARAMS, HOST_APP
import time
import os

doc = __eventargs__.Document
try:
    title = doc.Title
except:
    sys.exit()

savestatus_datafile = os.path.join(
    os.path.expandvars("%userprofile%\\AppData\\Roaming\\pyRevit\\Cache"),
    title + str(hash(doc.PathName)) + "_savestatus.txt",
)

with open(savestatus_datafile, "w") as f:
    f.write(str(title) + "|" + str(time.time()))