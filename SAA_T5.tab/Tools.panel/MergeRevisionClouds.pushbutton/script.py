# -*- coding: utf-8 -*-
__title__ = "Merge\nClouds"
__author__ = "JK_Sim"
__doc__ = """Version 1.2
Date: 22.12.2025
_____________________________________________________________________
Description:
Merge selected Revision Clouds into one cloud and input custom ID.
If input is empty, the comment from the first selected cloud is used.
_____________________________________________________________________
"""

import clr, math
from Autodesk.Revit.DB import *
from pyrevit import forms
from System.Collections.Generic import List

# ---------------------------------------------------------
# INITIALIZATION
# ---------------------------------------------------------
doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

MM_TO_FT = 1.0 / 304.8
THICKNESS = 10 * MM_TO_FT

# ---------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------

def reverse_if_needed(loop):
    """Ensure CCW orientation."""
    pts = []
    it = loop.GetCurveLoopIterator()
    while it.MoveNext():
        pts.append(it.Current.GetEndPoint(0))

    if len(pts) < 3:
        return loop

    area = sum(
        pts[i].X * pts[(i+1) % len(pts)].Y -
        pts[(i+1) % len(pts)].X * pts[i].Y
        for i in range(len(pts))
    ) * 0.5

    if area >= 0:
        rev = CurveLoop()
        curves = []
        it = loop.GetCurveLoopIterator()
        while it.MoveNext():
            curves.insert(0, it.Current.CreateReversed())
        for c in curves:
            rev.Append(c)
        return rev

    return loop

def loop_to_wafer(cl):
    """Extrude flattened loop upward."""
    flat = CurveLoop()
    it = cl.GetCurveLoopIterator()
    while it.MoveNext():
        c = it.Current
        s, e = c.GetEndPoint(0), c.GetEndPoint(1)
        flat.Append(Line.CreateBound(XYZ(s.X, s.Y, 0), XYZ(e.X, e.Y, 0)))

    return GeometryCreationUtilities.CreateExtrusionGeometry(
        [flat], XYZ.BasisZ, THICKNESS)

def boolean_union(solids):
    merged = solids[0]
    for s in solids[1:]:
        merged = BooleanOperationsUtils.ExecuteBooleanOperation(
            merged, s, BooleanOperationsType.Union)
    return merged

def get_top_face(s):
    faces = [f for f in s.Faces
             if isinstance(f, PlanarFace) and f.FaceNormal.Z > 0.9]
    if not faces:
        return None
    faces.sort(key=lambda x: x.Area, reverse=True)
    return faces[0]

def pick_outer_loop(face):
    loops = list(face.GetEdgesAsCurveLoops())
    if not loops:
        return None
    
    def perim(cl):
        L = 0.0
        it = cl.GetCurveLoopIterator()
        while it.MoveNext():
            L += it.Current.Length
        return L

    return max(loops, key=perim)

def get_cloud_loops(cloud):
    curves = list(cloud.GetSketchCurves())
    if not curves:
        raise Exception("Cloud has no sketch curves.")

    def key(p):
        return (round(p.X, 6), round(p.Y, 6), round(p.Z, 6))

    unused = set(curves)
    loops = []

    while unused:
        start = unused.pop()
        loop = CurveLoop()
        loop.Append(start)
        endpt = key(start.GetEndPoint(1))

        made = True
        while made:
            made = False
            remove = None
            rev_flag = False

            for c in unused:
                sp = key(c.GetEndPoint(0))
                ep = key(c.GetEndPoint(1))
                if sp == endpt:
                    remove, rev_flag = c, False
                    break
                if ep == endpt:
                    remove, rev_flag = c, True
                    break

            if remove:
                unused.remove(remove)
                if rev_flag:
                    loop.Append(remove.CreateReversed())
                    endpt = key(remove.GetEndPoint(0))
                else:
                    loop.Append(remove)
                    endpt = key(remove.GetEndPoint(1))
                made = True

        loops.append(loop)

    return loops

# ---------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------

selection = [doc.GetElement(id) for id in uidoc.Selection.GetElementIds()]
clouds = [c for c in selection if isinstance(c, RevisionCloud)]

if len(clouds) < 2:
    forms.alert("Please select at least 2 revision clouds to merge.", title="Selection Error")
    raise Exception("Select at least 2 revision clouds.")

# --- Logic for Fallback Comment ---
first_cloud = clouds[0]
p_first = first_cloud.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
fallback_comment = p_first.AsString() if p_first else ""

user_comment = forms.ask_for_string(
    default="",
    prompt="Please input ID number from T5 Change Register (Optional)",
    title="Merged Cloud Comments"
)

# Determine the final string to apply
final_comment = user_comment if user_comment else fallback_comment

view = doc.ActiveView
revision_id = clouds[0].RevisionId 

wafers = []
for cloud in clouds:
    try:
        loops = get_cloud_loops(cloud)
        for cl in loops:
            wafers.append(loop_to_wafer(cl))
    except:
        continue

if not wafers:
    raise Exception("Could not extract geometry from selected clouds.")

merged = boolean_union(wafers)
top = get_top_face(merged)
if not top:
    raise Exception("Top face not found.")

outer = pick_outer_loop(top)
if not outer:
    raise Exception("Could not determine outer loop.")

outer = reverse_if_needed(outer) 

curves = List[Curve]()
it = outer.GetCurveLoopIterator()
while it.MoveNext():
    curves.Add(it.Current)

with Transaction(doc, "Merge Revision Clouds") as t:
    t.Start()

    new_cloud = RevisionCloud.Create(doc, view, revision_id, curves)

    try:
        p_new = new_cloud.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
        if p_new and not p_new.IsReadOnly:
            p_new.Set(final_comment if final_comment else "")
    except:
        pass

    for c in clouds:
        doc.Delete(c.Id)

    t.Commit()