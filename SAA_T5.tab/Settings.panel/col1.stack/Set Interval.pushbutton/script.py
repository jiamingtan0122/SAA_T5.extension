from pyrevit.coreutils.ribbon import ICON_MEDIUM
from pyrevit import script
from pyrevit.userconfig import user_config
from pyrevit import forms

__context__ = "zero-doc"
__title__ = "Set Interval"
__author__ = "Alex D\'Aversa"

def config_autosave_interval():
    default_interval = user_config.autosave.interval / 60
    interval = forms.GetValueWindow.show(
        None,
        value_type="slider",
        default=default_interval,
        prompt="Set autosave interval in minutes:",
        title="Autosave Interval",
        min=5,
        max=240,
    )
    if interval:
        user_config.autosave.interval = int(interval * 60)
        user_config.save_changes()

config_autosave_interval()