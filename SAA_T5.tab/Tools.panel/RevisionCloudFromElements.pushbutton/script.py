# -*- coding: utf-8 -*-
__title__ = "Revision\nClouds"
__author__ = "JK_Sim"
__doc__ = """Version 3.8
Date: 22.12.2025
_____________________________________________________________________
Description:
Generate Revision Clouds based on selected elements (including Floors, 
Ceilings, and Rooms) in the ACTIVE VIEW.
_____________________________________________________________________
"""

import clr, math
from Autodesk.Revit.DB import *
from pyrevit import forms
from System.Collections.Generic import List
from Autodesk.Revit.DB import Transaction

doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

MM_TO_FT    = 1.0 / 304.8
OFFSET_DIST = 300 * MM_TO_FT
THICKNESS   = 10  * MM_TO_FT

# --------------------------- basic utils ----------------------------
def distinct_xy(points, prec=6):
    seen = {}
    for p in points:
        k = (round(p.X, prec), round(p.Y, prec))
        seen[k] = XYZ(p.X, p.Y, 0)
    return list(seen.values())

def convex_hull(points):
    if len(points) < 3: return points[:]
    pts = sorted(points, key=lambda p: (p.X, p.Y))
    def cross(o, a, b):
        return (a.X-o.X)*(b.Y-o.Y) - (a.Y-o.Y)*(b.X-o.X)
    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0: lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0: upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]

def curveloop_from_points(points):
    cl = CurveLoop()
    if len(points) < 2: return cl
    tol = doc.Application.ShortCurveTolerance
    for i in range(len(points)):
        p1, p2 = points[i], points[(i + 1) % len(points)]
        if p1.DistanceTo(p2) > tol:
            cl.Append(Line.CreateBound(p1, p2))
    return cl

# -------------------- clipping helpers --------------------
def get_clipping_rect(view):
    sb_param = view.get_Parameter(BuiltInParameter.VIEWER_VOLUME_OF_INTEREST_CROP)
    if sb_param and sb_param.AsElementId().IntegerValue > 0:
        sb = doc.GetElement(sb_param.AsElementId())
        bb = sb.get_BoundingBox(None)
        if bb: return (bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y)
    if view.CropBoxActive:
        bb = view.CropBox
        return (bb.Min.X, bb.Min.Y, bb.Max.X, bb.Max.Y)
    return None

def loop_to_xy_points(loop):
    pts = []
    it = loop.GetCurveLoopIterator()
    first, last = None, None
    tol = doc.Application.ShortCurveTolerance
    while it.MoveNext():
        c = it.Current
        p0 = c.GetEndPoint(0)
        if first is None: first = p0
        pts.append(XYZ(p0.X, p0.Y, 0))
        last = c.GetEndPoint(1)
    if last and first and last.DistanceTo(first) > tol:
        pts.append(XYZ(last.X, last.Y, 0))
    return pts

def clip_polygon_to_rect(points, xmin, ymin, xmax, ymax):
    if not points: return []
    def clip_edge(pts, inside, intersect):
        if not pts: return []
        out = []
        prev = pts[-1]
        prev_in = inside(prev)
        for curr in pts:
            curr_in = inside(curr)
            if curr_in:
                if not prev_in: out.append(intersect(prev, curr))
                out.append(curr)
            elif prev_in: out.append(intersect(prev, curr))
            prev, prev_in = curr, curr_in
        return out
    def inside_left(p):   return p.X >= xmin
    def inside_right(p):  return p.X <= xmax
    def inside_bottom(p): return p.Y >= ymin
    def inside_top(p):    return p.Y <= ymax
    def intersect_x(p1, p2, x_clip):
        dx = (p2.X - p1.X)
        t = (x_clip - p1.X) / dx if abs(dx) > 1e-9 else 0
        return XYZ(x_clip, p1.Y + t * (p2.Y - p1.Y), 0)
    def intersect_y(p1, p2, y_clip):
        dy = (p2.Y - p1.Y)
        t = (y_clip - p1.Y) / dy if abs(dy) > 1e-9 else 0
        return XYZ(p1.X + t * (p2.X - p1.X), y_clip, 0)
    pts_out = clip_edge(points, inside_left, lambda a, b: intersect_x(a, b, xmin))
    pts_out = clip_edge(pts_out, inside_right, lambda a, b: intersect_x(a, b, xmax))
    pts_out = clip_edge(pts_out, inside_bottom, lambda a, b: intersect_y(a, b, ymin))
    pts_out = clip_edge(pts_out, inside_top, lambda a, b: intersect_y(a, b, ymax))
    return pts_out

# ------------------ loop extractors ------------------------
def get_room_loops(room):
    loops = []
    try:
        segs = room.GetBoundarySegments(SpatialElementBoundaryOptions())
        for loop in segs:
            cl = CurveLoop()
            for seg in loop:
                c = seg.GetCurve()
                p0, p1 = c.GetEndPoint(0), c.GetEndPoint(1)
                cl.Append(Line.CreateBound(XYZ(p0.X, p0.Y, 0), XYZ(p1.X, p1.Y, 0)))
            if cl.NumberOfCurves() > 1: loops.append(cl)
    except: pass
    return loops

def get_boundary_from_geometry(el, view, orientation="top"):
    opt = Options()
    opt.ComputeReferences = True
    opt.DetailLevel = ViewDetailLevel.Medium
    opt.IncludeNonVisibleObjects = False
    
    geo = el.get_Geometry(opt)
    if not geo: return []
    
    solids = []
    for g in geo:
        if isinstance(g, Solid) and g.Volume > 1e-6:
            solids.append(g)
        elif isinstance(g, GeometryInstance):
            for sg in g.GetInstanceGeometry():
                if isinstance(sg, Solid) and sg.Volume > 1e-6:
                    solids.append(sg)

    loops = []
    for s in solids:
        faces = list(s.Faces)
        if orientation == "top":
            faces.sort(key=lambda f: f.Evaluate(UV(0.5,0.5)).Z, reverse=True)
        else:
            faces.sort(key=lambda f: f.Evaluate(UV(0.5,0.5)).Z)
            
        for f in faces:
            if isinstance(f, PlanarFace) and abs(f.FaceNormal.Z) > 0.9:
                for cl in f.GetEdgesAsCurveLoops():
                    flat_cl = CurveLoop()
                    for c in cl:
                        p0, p1 = c.GetEndPoint(0), c.GetEndPoint(1)
                        flat_cl.Append(Line.CreateBound(XYZ(p0.X, p0.Y, 0), XYZ(p1.X, p1.Y, 0)))
                    loops.append(flat_cl)
                break 
    return loops

def wall_convex_outline_points(el, view):
    opt = Options(); opt.IncludeNonVisibleObjects = False; opt.View = view
    geo = el.get_Geometry(opt)
    pts = []
    if geo:
        for g in geo:
            if isinstance(g, Solid) and g.Volume > 1e-9:
                for e in g.Edges:
                    c = e.AsCurve()
                    pts.extend([XYZ(c.GetEndPoint(0).X, c.GetEndPoint(0).Y, 0), XYZ(c.GetEndPoint(1).X, c.GetEndPoint(1).Y, 0)])
            elif isinstance(g, GeometryInstance):
                for sg in g.GetInstanceGeometry():
                    if isinstance(sg, Solid) and sg.Volume > 1e-9:
                        for e in sg.Edges:
                            c = e.AsCurve()
                            pts.extend([XYZ(c.GetEndPoint(0).X, c.GetEndPoint(0).Y, 0), XYZ(c.GetEndPoint(1).X, c.GetEndPoint(1).Y, 0)])
    if len(pts) < 3:
        bb = el.get_BoundingBox(None)
        if bb: pts = [XYZ(bb.Min.X, bb.Min.Y, 0), XYZ(bb.Max.X, bb.Min.Y, 0), XYZ(bb.Max.X, bb.Max.Y, 0), XYZ(bb.Min.X, bb.Max.Y, 0)]
    return convex_hull(distinct_xy(pts, 6))

def model_oriented_bbox_points(el, view):
    try: sym = el.Symbol
    except: sym = None
    bb = sym.get_BoundingBox(view) if sym else None
    if not bb: bb = el.get_BoundingBox(view) or el.get_BoundingBox(None)
    if not bb: return []
    box_local = [XYZ(bb.Min.X, bb.Min.Y, 0), XYZ(bb.Max.X, bb.Min.Y, 0), XYZ(bb.Max.X, bb.Max.Y, 0), XYZ(bb.Min.X, bb.Max.Y, 0)]
    try: T_inst = el.GetTransform()
    except: T_inst = Transform.Identity
    return [XYZ(p.X, p.Y, 0) for p in [T_inst.OfPoint(pt) for pt in box_local]]

def get_annotation_loops(el, view):
    bbox = el.get_BoundingBox(view)
    if not bbox: return []
    pts = [XYZ(bbox.Min.X, bbox.Min.Y, 0), XYZ(bbox.Max.X, bbox.Min.Y, 0), XYZ(bbox.Max.X, bbox.Max.Y, 0), XYZ(bbox.Min.X, bbox.Max.Y, 0)]
    return [curveloop_from_points(pts)]

def get_model_loops(el, view):
    cat_name = el.Category.Name if el.Category else ""
    if isinstance(el, Floor) or "Floor" in cat_name:
        return get_boundary_from_geometry(el, view, orientation="top")
    if "Ceiling" in cat_name:
        return get_boundary_from_geometry(el, view, orientation="bottom")
    
    pts = wall_convex_outline_points(el, view) if isinstance(el, Wall) else model_oriented_bbox_points(el, view)
    return [curveloop_from_points(pts)] if pts else []

# ----------------------------- ops -----------------------------------
def reverse_if_needed(loop):
    pts = []
    it = loop.GetCurveLoopIterator()
    while it.MoveNext(): pts.append(it.Current.GetEndPoint(0))
    if len(pts) < 3: return loop
    a = sum(pts[i].X * pts[(i+1)%len(pts)].Y - pts[(i+1)%len(pts)].X * pts[i].Y for i in range(len(pts))) * 0.5
    if a <= 0: return loop
    rev = CurveLoop(); curves = []
    it = loop.GetCurveLoopIterator()
    while it.MoveNext(): curves.insert(0, it.Current.CreateReversed())
    for c in curves: rev.Append(c)
    return rev

def offset_loop(loop, dist):
    try: return CurveLoop.CreateViaOffset(loop, dist, XYZ.BasisZ)
    except: return loop

def make_wafer(loop):
    try:
        flat = CurveLoop()
        it = loop.GetCurveLoopIterator()
        while it.MoveNext():
            s, e = it.Current.GetEndPoint(0), it.Current.GetEndPoint(1)
            flat.Append(Line.CreateBound(XYZ(s.X, s.Y, 0), XYZ(e.X, e.Y, 0)))
        return GeometryCreationUtilities.CreateExtrusionGeometry([flat], XYZ.BasisZ, THICKNESS)
    except: return None

def _try_union(a, b):
    try: return BooleanOperationsUtils.ExecuteBooleanOperation(a, b, BooleanOperationsType.Union)
    except:
        try: return BooleanOperationsUtils.ExecuteBooleanOperation(b, a, BooleanOperationsType.Union)
        except: return None

def merge_solids_groups(solids):
    if not solids: return []
    groups = []
    for s in solids:
        placed = False
        for i, g in enumerate(groups):
            u = _try_union(g, s)
            if u: groups[i] = u; placed = True; break
        if not placed: groups.append(s)
    return groups

def get_all_top_faces(solid):
    return [f for f in solid.Faces if isinstance(f, PlanarFace) and f.FaceNormal.Z > 0.9]

# ----------------------------- main ----------------------------------
model_view = doc.ActiveView
if isinstance(model_view, ViewSheet):
    forms.alert("Run this from a MODEL view.", title="Error")
    raise Exception("Wrong view type.")

sel_ids = uidoc.Selection.GetElementIds()
if not sel_ids:
    forms.alert("No elements selected.", title="Error")
    raise Exception("No selection.")

elements = [doc.GetElement(eid) for eid in sel_ids if doc.GetElement(eid)]

revisions = list(FilteredElementCollector(doc).OfClass(Revision))
rev_labels = ["Revision {} | {}".format(r.SequenceNumber, r.Description or "No Description") for r in sorted(revisions, key=lambda x: x.SequenceNumber, reverse=True)]
sel = forms.SelectFromList.show(rev_labels, title="Select Revision")
if not sel: raise Exception("User cancelled.")
revision = {r.SequenceNumber: r for r in revisions}[int(sel.split(" | ")[0].replace("Revision ", ""))]

user_comment = forms.ask_for_string(default="", prompt="Please input ID number (Optional)", title="Revision Cloud Comments")

all_loops = []
for el in elements:
    try:
        if isinstance(el, SpatialElement):
            loops = get_room_loops(el)
        elif el.Category and el.Category.CategoryType == CategoryType.Annotation:
            loops = get_annotation_loops(el, model_view)
        else:
            loops = get_model_loops(el, model_view)
        
        if loops: all_loops.extend(loops)
    except:
        continue

offset_loops = [offset_loop(cl, OFFSET_DIST) for cl in all_loops]
clip_rect = get_clipping_rect(model_view)
if clip_rect:
    xmin, ymin, xmax, ymax = clip_rect
    final_clipped = []
    for cl in offset_loops:
        pts = clip_polygon_to_rect(loop_to_xy_points(cl), xmin, ymin, xmax, ymax)
        if len(pts) >= 3: final_clipped.append(curveloop_from_points(pts))
    offset_loops = final_clipped

all_solids = [make_wafer(cl) for cl in offset_loops if make_wafer(cl)]
solid_groups = merge_solids_groups(all_solids)

tol = doc.Application.ShortCurveTolerance
max_gap = tol * 100.0
cloud_curve_sets = []

for s in solid_groups:
    faces = get_all_top_faces(s)
    for face in faces:
        loops = list(face.GetEdgesAsCurveLoops())
        if not loops: continue
        outer_loop = max(loops, key=lambda l: l.GetExactLength())
        final_loop = reverse_if_needed(outer_loop)
        
        curves = List[Curve]()
        first_start, prev_end = None, None
        it = final_loop.GetCurveLoopIterator()
        while it.MoveNext():
            c = it.Current
            if not c or c.Length < tol: continue
            spt, ept = c.GetEndPoint(0), c.GetEndPoint(1)
            if first_start is None: first_start = spt
            if prev_end and prev_end.DistanceTo(spt) < max_gap:
                try: curves.Add(Line.CreateBound(prev_end, spt))
                except: pass
            curves.Add(c); prev_end = ept
        
        if first_start and prev_end and prev_end.DistanceTo(first_start) < max_gap:
            try: curves.Add(Line.CreateBound(prev_end, first_start))
            except: pass
        
        if curves.Count > 0: 
            cloud_curve_sets.append(curves)

with Transaction(doc, "Create Revision Cloud") as t:
    t.Start()
    for curves in cloud_curve_sets:
        try:
            cloud = RevisionCloud.Create(doc, model_view, revision.Id, curves)
            p = cloud.get_Parameter(BuiltInParameter.ALL_MODEL_INSTANCE_COMMENTS)
            if p and not p.IsReadOnly:
                p.Set(user_comment if user_comment else "")
        except:
            pass
    t.Commit()