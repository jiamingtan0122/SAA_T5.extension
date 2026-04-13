from System.Collections.Generic import *
from pyrevit import revit, DB, UI
from pyrevit import script
from pyrevit import forms
from pyrevit import EXEC_PARAMS, HOST_APP
from pyrevit.userconfig import user_config

if user_config.has_section("autosave"):
    if user_config.autosave.has_option("interval"):
        # print('interval present')
        pass
    else:
        # set default value
        # print('interval not present')
        user_config.autosave.interval = 900
    if user_config.autosave.has_option("enabled"):
        # print('enabled present')
        pass
    else:
        # set default value
        # print('enabled not present')
        user_config.autosave.enabled = False
    user_config.save_changes()
else:
    user_config.add_section("autosave")
    user_config.autosave.interval = 900
    user_config.autosave.enabled = False
    user_config.save_changes()
