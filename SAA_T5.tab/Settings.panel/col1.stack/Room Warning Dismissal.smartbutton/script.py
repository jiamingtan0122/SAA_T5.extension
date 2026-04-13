# -*- coding: utf-8 -*-
__title__ = "RoomWarning"
__author__ = "JK_Sim"
__doc__ = """Version = 1.2
Date    = 03.11.2025
_____________________________________________________________________
Description:
Dismiss Multiple Rooms Warning and Room Tag Warnings.
Handler auto-registers on startup (no alert shown).
Alert appears only when the button is clicked manually.
_____________________________________________________________________
"""

from pyrevit import forms
from pyrevit.revit import HOST_APP
import clr

clr.AddReference("RevitAPI")
from Autodesk.Revit.DB import FailureProcessingResult
from Autodesk.Revit.DB.Events import FailuresProcessingEventArgs
from System import EventHandler

# Keep reference alive
event_handler = None

def handle_failures(sender, args):
    fa = args.GetFailuresAccessor()
    failures = fa.GetFailureMessages()
    to_delete = []

    for fm in failures:
        msg = fm.GetDescriptionText()
        if "Multiple Rooms are in the same enclosed region" in msg:
            to_delete.append(fm)
        elif "Room Tag is outside of its Room" in msg:
            to_delete.append(fm)

    for fm in to_delete:
        fa.DeleteWarning(fm)

    args.SetProcessingResult(FailureProcessingResult.Continue)

def register_handler():
    global event_handler
    app = HOST_APP.app

    if event_handler is None:
        event_handler = EventHandler[FailuresProcessingEventArgs](handle_failures)
        app.FailuresProcessing += event_handler

# Runs automatically on Revit/pyRevit startup
def __selfinit__(script_cmp, ui_button_cmp, __rvt__):
    register_handler()

# Runs only when user clicks the button
if __name__ == "__main__":
    forms.alert("Room warning dismissal is ON", title="Dismiss Warning")
