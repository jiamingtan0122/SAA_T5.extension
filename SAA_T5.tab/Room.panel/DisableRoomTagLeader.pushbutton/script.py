# -*- coding: utf-8 -*-

__title__ = "Disable\nTag Leader"

__author__ = "JM"

__doc__ = """Version = 1.1

Date    = 12.05.2025

_____________________________________________________________________

Description:



Disables the leader on ALL Room Tags in the active view.

Tag head positions are preserved after leader is removed.

_____________________________________________________________________

How-to:



-> Open the affected view

-> Run this script

-> Done

_____________________________________________________________________

"""

from Autodesk.Revit.DB import *
from pyrevit import revit, forms, script

doc = revit.doc
output = script.get_output()

# ------------------------------------- COLLECT ALL ROOM TAGS

collector = FilteredElementCollector(doc)\
    .OfCategory(BuiltInCategory.OST_RoomTags)\
    .WhereElementIsNotElementType()\
    .ToElements()

if not collector:
    forms.alert("No Room Tags found in this document.")
    script.exit()

# ------------------------------------- DISABLE LEADERS, PRESERVE POSITION

fixed = 0
skipped = 0

with Transaction(doc, "Disable Room Tag Leaders") as t:
    t.Start()
    try:
        for tag in collector:
            try:
                if tag.HasLeader:
                    # Step 1: capture current tag head position
                    current_pos = tag.Location.Point

                    # Step 2: disable leader (this moves the tag head)
                    tag.HasLeader = False

                    # Step 3: restore original tag head position
                    tag.Location.Move(
                        current_pos - tag.Location.Point
                    )
                    fixed += 1
                else:
                    skipped += 1
            except Exception as e:
                output.print_md("**Skipped tag {}**: {}".format(tag.Id, str(e)))
                skipped += 1
        t.Commit()
    except Exception as e:
        t.RollBack()
        forms.alert("Transaction failed: {}".format(str(e)))
        script.exit()

# ------------------------------------- REPORT

output.print_md("### Disable Room Tag Leaders — Done")
output.print_md