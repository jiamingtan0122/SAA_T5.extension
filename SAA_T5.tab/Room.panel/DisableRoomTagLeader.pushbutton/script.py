# -*- coding: utf-8 -*-
__title__ = "Disable\nTag Leader"
__author__ = "JM"
__doc__ = """Version = 1.4
Date    = 30.07.2026
_____________________________________________________________________
Description:
Disables the leader on selected Room Tags only, so you can keep the
leader on any tags that still need one.
Tag head positions are preserved after leader is removed, where the
room geometry allows it (Revit will not let a leader-less tag head
sit outside its host room, so a handful may fall back to the
room's crosshair position instead).
_____________________________________________________________________
How-to:
-> Select the Room Tags you want to fix (or run with nothing
   selected and you'll be prompted to pick them)
-> Run this script
-> Done
_____________________________________________________________________
"""
from Autodesk.Revit.DB import *
from Autodesk.Revit.UI import Selection
from pyrevit import revit, forms, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# ------------------------------------- GET ROOM TAGS FROM SELECTION
selection = revit.get_selection()
collector = [el for el in selection if el.Category
             and el.Category.Id.IntegerValue == int(BuiltInCategory.OST_RoomTags)]

if not collector:
    # nothing pre-selected (or selection had no room tags) -> prompt to pick
    try:
        picked_refs = uidoc.Selection.PickObjects(
            Selection.ObjectType.Element,
            forms.SelectionFilter(
                lambda el: el.Category is not None
                and el.Category.Id.IntegerValue == int(BuiltInCategory.OST_RoomTags)
            ),
            "Select Room Tags to remove the leader from"
        )
        collector = [doc.GetElement(r.ElementId) for r in picked_refs]
    except Exception:
        forms.alert("No Room Tags selected.")
        script.exit()

if not collector:
    forms.alert("No Room Tags selected.")
    script.exit()

total = len(collector)

# ------------------------------------- DISABLE LEADERS, PRESERVE POSITION
fixed = 0
fallback = 0
no_leader = 0

with Transaction(doc, "Disable Room Tag Leaders") as t:
    t.Start()
    with forms.ProgressBar(title="Removing tag leaders ({value} of {max_value})") as pb:
        for i, tag in enumerate(collector):
            if tag.HasLeader:
                original_pos = tag.TagHeadPosition
                tag.HasLeader = False
                try:
                    # try to restore the exact remembered position
                    tag.TagHeadPosition = original_pos
                    fixed += 1
                except Exception:
                    # position was outside the room -> Revit keeps the
                    # crosshair position it snapped to instead
                    fallback += 1
            else:
                no_leader += 1
            pb.update_progress(i + 1, total)
    t.Commit()

# ------------------------------------- REPORT
output.print_md(
    "**Done** — {} tags total | {} restored to original position | "
    "{} snapped to room crosshair (couldn't sit outside room) | {} already had no leader"
    .format(total, fixed, fallback, no_leader)
)