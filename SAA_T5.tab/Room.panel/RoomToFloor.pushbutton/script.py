# -*- coding: utf-8 -*-
__title__ = "Room To\nFloor"
__author__ = "YourName"
__doc__ = """Version = 9.0
Date    = 12.12.2025
Description:
Generates Floors from selected Rooms.
- Step 1: Select Floor Type.
- Step 2: Enter Offset.
- Logic: Merges Room Boundary with Door Openings (Full Sill).
"""

from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, ElementId, RevitLinkInstance, 
    SpatialElement, SpatialElementBoundaryOptions, 
    SpatialElementBoundaryLocation, FilteredElementCollector, 
    Floor, FloorType, Level, Transaction, CurveLoop, Curve, Line, XYZ,
    GeometryCreationUtilities, BooleanOperationsUtils, BooleanOperationsType,
    Solid, PlanarFace, Transform, Wall
)
from pyrevit import revit, forms, script
import math

# ---------------- INITIALIZATION ----------------
doc = revit.doc
uidoc = revit.uidoc

# ---------------- HELPERS ----------------

def flatten_pt(pt):
    """Forces a point to exactly Z=0."""
    return XYZ(pt.X, pt.Y, 0.0)

def get_type_name_safe(element):
    param = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
    if param and param.HasValue:
        return param.AsString()
    try: return element.Name
    except: return "Unnamed Type"

def get_all_floor_types():
    return FilteredElementCollector(doc).OfClass(FloorType).WhereElementIsElementType().ToElements()

def get_all_levels():
    return FilteredElementCollector(doc).OfClass(Level).ToElements()

def get_level_by_name(name, all_levels):
    for lvl in all_levels:
        if lvl.Name == name: return lvl
    return None

def get_room_side_direction(door, room, check_dist=0.5):
    """
    Determines if the room is on the 'Facing' side (1) or 'Opposite' side (-1).
    """
    try:
        pt_center = door.Location.Point
        facing = door.FacingOrientation.Normalize()
        
        # Probe Point A (Facing Direction)
        pt_A = pt_center + (facing * check_dist)
        if room.IsPointInRoom(pt_A): return 1
            
        # Probe Point B (Opposite Direction)
        pt_B = pt_center - (facing * check_dist)
        if room.IsPointInRoom(pt_B): return -1
            
        return 0
    except:
        return 0

def get_wall_thickness(wall):
    try: return wall.Width
    except: return 0.5 

def get_opening_solid(door, wall_thickness, mode, room_direction, total_transform=None):
    """
    Creates a solid plug based on Extension Mode.
    """
    try:
        # 1. Dimensions
        width = 3.0 
        p_w = door.get_Parameter(BuiltInParameter.CASEWORK_WIDTH)
        if not p_w or not p_w.HasValue:
             sym = door.Symbol
             p_w = sym.get_Parameter(BuiltInParameter.DOOR_WIDTH)
        if p_w and p_w.HasValue: width = p_w.AsDouble()
        
        # 2. Location
        loc = door.Location
        if not hasattr(loc, "Point"): return None
        center_pt = loc.Point 
        facing = door.FacingOrientation 
        hand = door.HandOrientation 
        
        # 3. Logic
        plug_depth = 0.0
        center_offset_dist = 0.0
        
        if mode == "Full Sill":
            # FULL SILL: Covers full wall thickness centered on wall
            plug_depth = wall_thickness
            center_offset_dist = 0.0
            
        elif mode == "Half Sill":
            # HALF SILL: Covers from Wall Center to Room Face
            plug_depth = wall_thickness / 2.0
            # Shift towards room by quarter thickness
            center_offset_dist = (wall_thickness / 4.0) * room_direction
            
        else:
            return None 

        half_w = width / 2.0
        half_d = plug_depth / 2.0
        
        plug_center = center_pt + (facing * center_offset_dist)
        
        # 4. Corners
        p1 = plug_center + (hand * half_w) + (facing * half_d)
        p2 = plug_center - (hand * half_w) + (facing * half_d)
        p3 = plug_center - (hand * half_w) - (facing * half_d)
        p4 = plug_center + (hand * half_w) - (facing * half_d)
        
        pts = [p1, p2, p3, p4]
        
        if total_transform:
            pts = [total_transform.OfPoint(p) for p in pts]
            
        pts_flat = [flatten_pt(p) for p in pts]
        
        # 5. Extrude
        loop = CurveLoop()
        loop.Append(Line.CreateBound(pts_flat[0], pts_flat[1]))
        loop.Append(Line.CreateBound(pts_flat[1], pts_flat[2]))
        loop.Append(Line.CreateBound(pts_flat[2], pts_flat[3]))
        loop.Append(Line.CreateBound(pts_flat[3], pts_flat[0]))
        
        return GeometryCreationUtilities.CreateExtrusionGeometry([loop], XYZ.BasisZ, 1.0)

    except:
        return None

def get_room_boundary_solid(room, transform=None):
    """Returns room boundary as a solid at Z=0."""
    options = SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Finish
    
    loops = []
    bounding_wall_ids = set()
    
    try:
        segments_list = room.GetBoundarySegments(options)
        if not segments_list: return None, None

        for segments in segments_list:
            curve_loop = CurveLoop()
            for seg in segments:
                w_id = seg.ElementId
                if w_id != ElementId.InvalidElementId:
                    bounding_wall_ids.add(w_id)

                curve = seg.GetCurve()
                if transform:
                    curve = curve.CreateTransformed(transform)
                
                # Flatten
                p0 = flatten_pt(curve.GetEndPoint(0))
                p1 = flatten_pt(curve.GetEndPoint(1))
                if isinstance(curve, Line):
                    curve_loop.Append(Line.CreateBound(p0, p1))
                else:
                    curve_loop.Append(Line.CreateBound(p0, p1))

            if curve_loop.IsOpen():
                try:
                    curves = [c for c in curve_loop]
                    p_start = curves[0].GetEndPoint(0)
                    p_end = curves[-1].GetEndPoint(1)
                    if p_start.DistanceTo(p_end) > 0.003:
                        curve_loop.Append(Line.CreateBound(p_end, p_start))
                except: pass
            
            loops.append(curve_loop)
        
        return GeometryCreationUtilities.CreateExtrusionGeometry(loops, XYZ.BasisZ, 1.0), bounding_wall_ids
    except:
        return None, None

def get_door_opening_solids(room, room_doc, bounding_wall_ids, mode, total_transform=None):
    if mode == "None": return []
    
    door_solids = []
    if not bounding_wall_ids: return []
    
    doors = FilteredElementCollector(room_doc)\
            .OfCategory(BuiltInCategory.OST_Doors)\
            .WhereElementIsNotElementType()\
            .ToElements()
            
    for door in doors:
        if door.Host and door.Host.Id in bounding_wall_ids:
            
            # Check Sill
            sill_val = 0.0
            p_sill = door.get_Parameter(BuiltInParameter.INSTANCE_SILL_HEIGHT_PARAM)
            if p_sill and p_sill.HasValue: sill_val = p_sill.AsDouble()
            
            if abs(sill_val) < 0.03:
                r_dir = get_room_side_direction(door, room)
                
                if r_dir != 0:
                    w_thick = get_wall_thickness(door.Host)
                    d_solid = get_opening_solid(door, w_thick, mode, r_dir, total_transform)
                    
                    if d_solid:
                        door_solids.append(d_solid)

    return door_solids

def merge_geometries(room_solid, door_solids):
    if not room_solid: return None
    if not door_solids: return room_solid
    
    current_solid = room_solid
    for d_solid in door_solids:
        try:
            current_solid = BooleanOperationsUtils.ExecuteBooleanOperation(
                current_solid, d_solid, BooleanOperationsType.Union
            )
        except: pass
    return current_solid

def extract_loops_from_solid(solid):
    if not solid: return None
    for face in solid.Faces:
        if isinstance(face, PlanarFace):
            if face.FaceNormal.IsAlmostEqualTo(XYZ(0,0,-1)):
                return face.GetEdgesAsCurveLoops()
    return None

# ---------------- MAIN LOGIC ----------------

sel_refs = uidoc.Selection.GetReferences()
if not sel_refs:
    forms.alert("Please select Room(s) first.", exitscript=True)

# --- SAFE UI ---

# 1. Floor Type
all_floor_types = get_all_floor_types()
floor_types_dict = {get_type_name_safe(ft): ft for ft in all_floor_types if get_type_name_safe(ft)}
if not floor_types_dict: forms.alert("No Floor Types.", exitscript=True)

selected_type_name = forms.SelectFromList.show(
    sorted(floor_types_dict.keys()),
    title="Select Floor Type",
    multiselect=False
)
if not selected_type_name: script.exit()
selected_floor_type = floor_types_dict[selected_type_name]

# 2. Offset
offset_str = forms.ask_for_string(
    default="0.0",
    prompt="Enter Height Offset (mm):",
    title="Height Offset"
)
if not offset_str: script.exit()
try:
    height_offset_feet = float(offset_str) / 304.8
except:
    forms.alert("Invalid Number.", exitscript=True)

# 3. Sill Logic (DEFAULT)
extension_mode = "Full Sill"


# --- PROCESSING ---

floors_created = 0
all_levels = get_all_levels()

t = Transaction(doc, "Create Floors")
t.Start()

for ref in sel_refs:
    room_element = None
    transform = None
    target_level_id = None
    room_doc = doc 
    
    if ref.LinkedElementId != ElementId.InvalidElementId:
        link_inst = doc.GetElement(ref.ElementId)
        if isinstance(link_inst, RevitLinkInstance):
            link_doc = link_inst.GetLinkDocument()
            if link_doc:
                room_element = link_doc.GetElement(ref.LinkedElementId)
                transform = link_inst.GetTotalTransform()
                room_doc = link_doc
    else:
        room_element = doc.GetElement(ref.ElementId)

    if room_element and isinstance(room_element, SpatialElement):
        
        # Level Logic
        if transform: 
            l_level = room_doc.GetElement(room_element.LevelId)
            h_level = get_level_by_name(l_level.Name, all_levels)
            if h_level: target_level_id = h_level.Id
            else:
                 if doc.ActiveView.GenLevel: target_level_id = doc.ActiveView.GenLevel.Id
                 else: continue
        else:
            target_level_id = room_element.LevelId

        try:
            # 1. Room
            room_solid, wall_ids = get_room_boundary_solid(room_element, transform)
            
            if room_solid:
                final_solid = room_solid
                
                # 2. Doors (Defaulting to Full Sill)
                door_solids = get_door_opening_solids(room_element, room_doc, wall_ids, extension_mode, transform)
                
                # 3. Merge
                final_solid = merge_geometries(room_solid, door_solids)
                
                # 4. Extract
                final_loops = extract_loops_from_solid(final_solid)
                
                if final_loops:
                    new_floor = Floor.Create(doc, final_loops, selected_floor_type.Id, target_level_id)
                    if height_offset_feet != 0.0:
                        p = new_floor.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
                        if p: p.Set(height_offset_feet)
                    floors_created += 1

        except Exception:
            pass

t.Commit()

if floors_created > 0:
    pass 
else:
    forms.alert("No floors created.", warn_icon=True)