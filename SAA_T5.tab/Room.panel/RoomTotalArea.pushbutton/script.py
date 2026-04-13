# -*- coding: utf-8 -*-
__title__ = "Total\nArea"
__author__ = "JK_Sim"
__doc__ = """Version = 6.1
Date    = 20.01.2026
Description:
Calculates total area of selected Rooms and Areas.
- Seamlessly handles Native and Linked elements.
- No pop-ups or confirmations.
- Uses Reference selection to identify specific linked elements.
"""

from Autodesk.Revit.DB import (
    BuiltInCategory, ElementId, RevitLinkInstance, SpatialElement
)
from pyrevit import revit, forms, script

# ---------------- INITIALIZATION ----------------
doc = revit.doc
uidoc = revit.uidoc

# ---------------- HELPERS ----------------
def get_internal_area(element):
    """Safely returns area if element is placed and valid."""
    if hasattr(element, "Area") and element.Area > 0:
        return element.Area
    return 0.0

def to_sqm(sqft):
    return sqft * 0.09290304

# ---------------- MAIN LOGIC ----------------

sel_refs = uidoc.Selection.GetReferences()

if not sel_refs:
    forms.alert("Please select Room(s) or Area(s) first.", exitscript=True)

total_area_sqft = 0.0
element_count = 0

VALID_CATEGORIES = (
    int(BuiltInCategory.OST_Rooms),
    int(BuiltInCategory.OST_Areas)
)

for ref in sel_refs:
    element = None

    # --- CASE A: Linked Element ---
    if ref.LinkedElementId != ElementId.InvalidElementId:
        link_inst = doc.GetElement(ref.ElementId)
        if isinstance(link_inst, RevitLinkInstance):
            link_doc = link_inst.GetLinkDocument()
            if link_doc:
                element = link_doc.GetElement(ref.LinkedElementId)

    # --- CASE B: Native Element ---
    else:
        element = doc.GetElement(ref.ElementId)

    # --- PROCESS ELEMENT ---
    if (
        element
        and isinstance(element, SpatialElement)
        and element.Category
        and element.Category.Id.IntegerValue in VALID_CATEGORIES
    ):
        area = get_internal_area(element)
        if area > 0:
            total_area_sqft += area
            element_count += 1

# ---------------- OUTPUT ----------------
if element_count == 0:
    forms.alert("No placed Rooms or Areas found in selection.", warn_icon=True)
    script.exit()

val_sqm = to_sqm(total_area_sqft)

msg = (
    "Total Area Calculation\n"
    "---------------------------\n"
    "Elements Selected: {}\n"
    "---------------------------\n"
    "{:.2f} m²\n"
    "{:.2f} sqft"
).format(element_count, val_sqm, total_area_sqft)

forms.alert(msg, title="Total Area")
