# -*- coding: utf-8 -*-
__title__ = "Disable\nTag Leader"
__author__ = "JM"
__doc__ = """Version = 1.6
Date    = 23.10.2026
_____________________________________________________________________
Description:
Disables the leader on selected Room Tags whose tag head sits inside
its room boundary. Tags whose head sits outside the room keep their
leader. Tag head positions are preserved after the leader is removed.
_____________________________________________________________________
How-to:
-> Select the Room Tags you want to fix, OR select views / sheets
   in the Project Browser to process every Room Tag in them
   (or run with nothing selected and you'll be prompted to pick tags)
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
ROOM_TAG_CAT = int(BuiltInCategory.OST_RoomTags)


def id_int(eid):
    # ElementId.IntegerValue was removed in Revit 2026 -> use .Value there
    try:
        return eid.Value
    except AttributeError:
        return eid.IntegerValue


selection = revit.get_selection()
collector = [el for el in selection if el.Category
             and id_int(el.Category.Id) == ROOM_TAG_CAT]

if not collector:
    # views / sheets selected in the Project Browser -> all room tags in them
    views = []
    for el in selection:
        if isinstance(el, ViewSheet):
            views.extend(doc.GetElement(vid) for vid in el.GetAllPlacedViews())
        elif isinstance(el, View) and not el.IsTemplate:
            views.append(el)

    seen = set()
    for v in views:
        for tag in FilteredElementCollector(doc, v.Id) \
                .OfCategory(BuiltInCategory.OST_RoomTags) \
                .WhereElementIsNotElementType():
            if id_int(tag.Id) not in seen:
                seen.add(id_int(tag.Id))
                collector.append(tag)

    if views and not collector:
        forms.alert("No Room Tags found in the {} selected view(s).".format(len(views)))
        script.exit()

if not collector:
    # nothing pre-selected (or selection had no room tags) -> prompt to pick
    try:
        picked_refs = uidoc.Selection.PickObjects(
            Selection.ObjectType.Element,
            forms.SelectionFilter(
                lambda el: el.Category is not None
                and id_int(el.Category.Id) == ROOM_TAG_CAT
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


def head_inside_room(tag):
    """True if the tag head lies within the tagged room's boundary."""
    room = tag.Room
    if room is None:
        # linked-model room or unplaced -> treat as outside (keep leader)
        return False
    p = tag.TagHeadPosition
    bb = room.get_BoundingBox(None)
    # tag head Z may not sit within the room volume -> test at room mid-height
    z = (bb.Min.Z + bb.Max.Z) / 2.0 if bb else p.Z
    return room.IsPointInRoom(XYZ(p.X, p.Y, z))


# ------------------------------------- DISABLE LEADERS, PRESERVE POSITION
fixed = 0
fallback = 0
no_leader = 0
kept = 0
failed = []

with Transaction(doc, "Disable Room Tag Leaders") as t:
    t.Start()
    with forms.ProgressBar(title="Removing tag leaders ({value} of {max_value})") as pb:
        for i, tag in enumerate(collector):
            if not tag.HasLeader:
                no_leader += 1
            elif not head_inside_room(tag):
                # tag head outside room -> keep the leader
                kept += 1
            else:
                original_pos = tag.TagHeadPosition
                try:
                    tag.HasLeader = False
                except Exception:
                    # Revit's own host check disagrees with ours
                    # ("Pinned tag head position must be located in the
                    # host element") -> skip, leave the leader on
                    failed.append(tag.Id)
                    pb.update_progress(i + 1, total)
                    continue
                try:
                    # try to restore the exact remembered position
                    tag.TagHeadPosition = original_pos
                    fixed += 1
                except Exception:
                    # Revit rejected the position -> keeps the
                    # crosshair position it snapped to instead
                    fallback += 1
            pb.update_progress(i + 1, total)
    t.Commit()

# ------------------------------------- REPORT
output.print_md(
    "**Done** — {} tags total | {} leaders removed (inside room) | "
    "{} leaders kept (head outside room) | {} snapped to room crosshair | "
    "{} already had no leader"
    .format(total, fixed, kept, fallback, no_leader)
)
if failed:
    output.print_md(
        "**{} tags skipped** — Revit refused to remove the leader "
        "(tag head not inside host room by Revit's check). "
        "Move the tag head into the room and re-run:".format(len(failed))
    )
    print(output.linkify(failed))