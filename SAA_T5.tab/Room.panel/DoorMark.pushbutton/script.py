# -*- coding: utf-8 -*-
__title__ = "Door\nMark"
__author__ = "JK_Sim"
__doc__ = """Version = 1.6
Date    = 02.11.2025
_____________________________________________________________________
Description:

Fill in Door parameter "Mark" and "ELEMENT_ROOM ALLOCATION"
from Room "Number" and "Name", including linked rooms.

Doors are sorted in two groups based on angle:
  • Group 1 (Top/Bottom sides): clockwise
  • Group 2 (Left/Right sides): anti-clockwise
Reference center is the room centroid.
_____________________________________________________________________
How-to:

-> Select Doors & ONLY 1 Room (host or from link)
-> Run this script
-> Done
_____________________________________________________________________
"""

from Autodesk.Revit.DB import *
from Autodesk.Revit.UI.Selection import ObjectType
from pyrevit import revit, forms, script
import math

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ"

# ------------------------------------- INIT
sel_ids = uidoc.Selection.GetElementIds()
sel_refs = uidoc.Selection.GetReferences()

selected = [doc.GetElement(id) for id in sel_ids]

doors = []
room = None

# --- First pass: check host model
for el in selected:
    if el.Category and el.Category.Id.IntegerValue == int(BuiltInCategory.OST_Doors):
        doors.append(el)
    elif el.Category and el.Category.Id.IntegerValue == int(BuiltInCategory.OST_Rooms):
        room = el

# --- Second pass: check if a linked room is selected
if not room:
    for ref in sel_refs:
        if ref.LinkedElementId != ElementId.InvalidElementId:
            link_inst = doc.GetElement(ref.ElementId)
            if isinstance(link_inst, RevitLinkInstance):
                link_doc = link_inst.GetLinkDocument()
                linked_elem = link_doc.GetElement(ref.LinkedElementId)
                if linked_elem.Category and linked_elem.Category.Id.IntegerValue == int(BuiltInCategory.OST_Rooms):
                    room = linked_elem
                    break

# ------------------------------------- VALIDATION
if not doors:
    forms.alert("No doors selected.")
    script.exit()

if room is None:
    forms.alert("No room found. Please select ONE room (host or linked).")
    script.exit()

p_num = room.LookupParameter("Number")
p_name = room.LookupParameter("Name")

if not p_num or not p_name:
    forms.alert("Room missing 'Number' or 'Name' parameter.")
    script.exit()

room_num = p_num.AsString() or ""
room_name = p_name.AsString() or ""

if not room_num or not room_name:
    forms.alert("Room Number or Name is empty.")
    script.exit()

# ------------------------------------- CENTER FROM ROOM CENTROID
room_loc = room.Location
bbox = room.get_BoundingBox(None)
if bbox:
    center = (bbox.Min + bbox.Max) / 2.0
else:
    forms.alert("Cannot determine room center.")
    script.exit()

# ------------------------------------- CLOCKWISE + ANTICLOCKWISE GROUP SORT
def compute_angle(center, pt):
    dx = pt.X - center.X
    dy = pt.Y - center.Y
    angle = math.degrees(math.atan2(dy, dx))
    angle = (360 - angle + 360) % 360     # Clockwise
    angle = (angle - 315) % 360           # Top-left = 0
    return angle

group1 = []  # Horizontal (top/bottom)
group2 = []  # Vertical (left/right)

for door in doors:
    loc = door.Location
    if isinstance(loc, LocationPoint):
        pt = loc.Point
        angle = compute_angle(center, pt)
        if angle < 135 or angle >= 315:
            group1.append((door, angle))
        else:
            group2.append((door, angle))

# Sort each group separately
group1_sorted = sorted(group1, key=lambda d: d[1])     # clockwise
group2_sorted = sorted(group2, key=lambda d: -d[1])    # anti-clockwise

# Merge final order
sorted_doors = [d[0] for d in group1_sorted + group2_sorted]

# ------------------------------------- ASSIGN VALUES
with Transaction(doc, "Door Mark from Room") as t:
    t.Start()

    for i, door in enumerate(sorted_doors):
        if i >= len(ALPHABET):
            output.print_md("Too many doors (max {}). Stopped at {}.".format(len(ALPHABET), i))
            break

        mark = room_num + ALPHABET[i]

        # Assign parameters
        p_mark = door.LookupParameter("Mark")
        if p_mark and not p_mark.IsReadOnly:
            p_mark.Set(mark)

        p_alloc = door.LookupParameter("ELEMENT_ROOM ALLOCATION")
        if p_alloc and not p_alloc.IsReadOnly:
            p_alloc.Set(room_name)

    t.Commit()
