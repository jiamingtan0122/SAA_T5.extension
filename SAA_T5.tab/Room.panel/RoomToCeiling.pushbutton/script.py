# -*- coding: utf-8 -*-
__title__ = "Room To\nCeiling"
__author__ = "YourName"
__doc__ = """Version = 1.0
Date    = 12.12.2025
Description:
Generates Ceilings from selected Rooms.
- Step 1: Select Rooms.
- Step 2: Select Ceiling Type.
- Step 3: Enter Height (Default 3000mm).
- Feature: Auto-closes open loops for Linked Rooms.
"""

from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, ElementId, RevitLinkInstance, 
    SpatialElement, SpatialElementBoundaryOptions, 
    SpatialElementBoundaryLocation, FilteredElementCollector, 
    Ceiling, CeilingType, Level, Transaction, CurveLoop, Curve, Line, XYZ
)
from pyrevit import revit, forms, script

# ---------------- INITIALIZATION ----------------
doc = revit.doc
uidoc = revit.uidoc

# ---------------- HELPERS ----------------

def get_type_name_safe(element):
    """Safely retrieves the name of an ElementType."""
    param = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
    if param and param.HasValue:
        return param.AsString()
    try:
        return element.Name
    except:
        return "Unnamed Type"

def get_all_ceiling_types():
    """Collects all available Ceiling Types."""
    return FilteredElementCollector(doc)\
        .OfClass(CeilingType)\
        .WhereElementIsElementType()\
        .ToElements()

def get_all_levels():
    return FilteredElementCollector(doc).OfClass(Level).ToElements()

def get_level_by_name(name, all_levels):
    for lvl in all_levels:
        if lvl.Name == name:
            return lvl
    return None

def get_boundary_loops(room, transform=None):
    """
    Returns CurveLoops for room boundaries (Finish).
    Attempts to close open loops by connecting start/end points.
    """
    options = SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Finish
    
    loops = []
    try:
        boundary_segments_list = room.GetBoundarySegments(options)
        if not boundary_segments_list:
            return None

        for segments in boundary_segments_list:
            curve_loop = CurveLoop()
            
            # 1. Collect all curves
            for seg in segments:
                curve = seg.GetCurve()
                if transform:
                    curve = curve.CreateTransformed(transform)
                curve_loop.Append(curve)
            
            # 2. Check and Fix Open Loops
            if curve_loop.IsOpen():
                try:
                    count = 0
                    first_curve = None
                    last_curve = None
                    for c in curve_loop:
                        if count == 0: first_curve = c
                        last_curve = c
                        count += 1
                    
                    if first_curve and last_curve:
                        start_pt = first_curve.GetEndPoint(0)
                        end_pt = last_curve.GetEndPoint(1)
                        
                        if start_pt.DistanceTo(end_pt) > 0.003: # Tolerance ~1mm
                            closing_line = Line.CreateBound(end_pt, start_pt)
                            curve_loop.Append(closing_line)
                except Exception as e:
                    print("Could not auto-close loop: {}".format(e))
                
            loops.append(curve_loop)
        return loops
    except:
        return None

# ---------------- MAIN LOGIC ----------------

# 1. Selection Validation
sel_refs = uidoc.Selection.GetReferences()
if not sel_refs:
    forms.alert("Please select Room(s) first.", exitscript=True)

# 2. Gather Data
all_ceiling_types = get_all_ceiling_types()
ceiling_types_dict = {}

for ct in all_ceiling_types:
    t_name = get_type_name_safe(ct)
    if t_name:
        ceiling_types_dict[t_name] = ct

if not ceiling_types_dict:
    forms.alert("No Ceiling Types found.", exitscript=True)

all_levels = get_all_levels()
levels_dict = {l.Name: l for l in all_levels}

# ---------------- USER INPUTS ----------------

# Input 1: Ceiling Type
selected_type_name = forms.SelectFromList.show(
    sorted(ceiling_types_dict.keys()),
    title="Select Ceiling Type",
    multiselect=False
)

if not selected_type_name:
    script.exit()

selected_ceiling_type = ceiling_types_dict[selected_type_name]

# Input 2: Height Offset
offset_str = forms.ask_for_string(
    default="3000",
    prompt="Enter Height Offset (mm):",
    title="Ceiling Height"
)

if offset_str is None:
    script.exit()

try:
    # Convert mm to feet (Revit Internal Units)
    val_mm = float(offset_str)
    height_offset_feet = val_mm / 304.8
except ValueError:
    forms.alert("Invalid number entered.", exitscript=True)


# ---------------- PROCESSING ----------------

ceilings_created = 0
errors = []
fallback_level_map = {} 

t = Transaction(doc, "Create Ceilings from Rooms")
t.Start()

for ref in sel_refs:
    room_element = None
    transform = None
    target_level_id = None
    
    # --- CASE A: Linked Element ---
    if ref.LinkedElementId != ElementId.InvalidElementId:
        link_inst = doc.GetElement(ref.ElementId)
        if isinstance(link_inst, RevitLinkInstance):
            link_doc = link_inst.GetLinkDocument()
            if link_doc:
                room_element = link_doc.GetElement(ref.LinkedElementId)
                transform = link_inst.GetTotalTransform()
                
                if room_element:
                    linked_level_id = room_element.LevelId
                    linked_level = link_doc.GetElement(linked_level_id)
                    linked_level_name = linked_level.Name
                    
                    # Level Mapping
                    host_level = get_level_by_name(linked_level_name, all_levels)
                    if host_level:
                        target_level_id = host_level.Id
                    else:
                        if linked_level_name in fallback_level_map:
                            target_level_id = fallback_level_map[linked_level_name]
                        else:
                            selected_lvl_name = forms.SelectFromList.show(
                                sorted(levels_dict.keys()),
                                title="Link Level '{}' missing. Pick Host Level:".format(linked_level_name),
                                multiselect=False
                            )
                            if selected_lvl_name:
                                selected_lvl = levels_dict[selected_lvl_name]
                                target_level_id = selected_lvl.Id
                                fallback_level_map[linked_level_name] = target_level_id
                            else:
                                errors.append("No host level selected for {}".format(linked_level_name))
                                continue

    # --- CASE B: Native Element ---
    else:
        room_element = doc.GetElement(ref.ElementId)
        if room_element:
            target_level_id = room_element.LevelId

    # --- GENERATION ---
    if room_element and isinstance(room_element, SpatialElement):
        if room_element.Category and room_element.Category.Id.IntegerValue == int(BuiltInCategory.OST_Rooms):
            
            curve_loops = get_boundary_loops(room_element, transform)
            
            if curve_loops and target_level_id:
                try:
                    # Create Ceiling (Revit 2022+ API)
                    # Note: Ceilings are created with Create(doc, curveLoops, typeId, levelId)
                    new_ceiling = Ceiling.Create(doc, curve_loops, selected_ceiling_type.Id, target_level_id)
                    
                    # Apply Offset (Height Offset From Level)
                    # Parameter: Height Offset From Level (BuiltInParameter.CEILING_HEIGHTABOVELEVEL_PARAM)
                    if new_ceiling:
                        param = new_ceiling.get_Parameter(BuiltInParameter.CEILING_HEIGHTABOVELEVEL_PARAM)
                        if param:
                            param.Set(height_offset_feet)
                            
                    ceilings_created += 1
                except Exception as e:
                    errors.append("Room {}: {}".format(room_element.Id, e))

t.Commit()

# ---------------- OUTPUT ----------------
if ceilings_created == 0:
    msg = "No ceilings were created."
    if errors:
        msg += "\nErrors:\n" + "\n".join(errors[:5])
    forms.alert(msg, title="Result", warn_icon=True)