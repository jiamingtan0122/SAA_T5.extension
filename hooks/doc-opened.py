# Reset the autosave timer when a document is opened (see view-activated.py)
import os
import time
from pyrevit.userconfig import user_config

try:
    doc = __eventargs__.Document
    if (user_config.autosave.get_option("enabled")
            and not doc.IsDetached
            and not doc.IsFamilyDocument
            and doc.PathName):
        path = os.path.join(
            os.path.expandvars("%userprofile%\AppData\Roaming\pyRevit\Cache"),
            doc.Title + str(hash(doc.PathName)) + "_savestatus.txt",
        )
        with open(path, "w") as f:
            f.write(str(doc.Title) + "|" + str(time.time()))
except Exception:
    pass
