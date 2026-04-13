# -*- coding: utf-8 -*-
__title__ = "Straighten\nWall"
__author__ = "JK_Sim"
__doc__ = """
Version 1.0 (18 Nov 2025)
Rotate selected walls so their orientation matches
the nearest grid segment direction (no translation).
"""

from Autodesk.Revit.DB import *
from pyrevit import forms
import math

doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument


# ---------------------------------------------------------
# Utility: Get direction vector of a curve (normalized)
# ---------------------------------------------------------
def curve_direction(curve):
    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    v = (p1 - p0)
    return v.Normalize()


# ---------------------------------------------------------
# Utility: signed angle between vectors (in XY plane)
# ---------------------------------------------------------
def signed_angle(v1, v2):
    # Ensure Z = 0 for 2D orientation work
    v1 = XYZ(v1.X, v1.Y, 0).Normalize()
    v2 = XYZ(v2.X, v2.Y, 0).Normalize()

    dot = max(-1.0, min(1.0, v1.DotProduct(v2)))
    angle = math.acos(dot)

    # Determine rotation sign using 2D cross-product (Z-component)
    cross_z = v1.X * v2.Y - v1.Y * v2.X
    if cross_z < 0:
        angle = -angle

    return angle


# ---------------------------------------------------------
# Collect ALL grid segment curves (handles jogged grids)
# ---------------------------------------------------------
def get_all_grid_segments(doc):
    segments = []
    grids = FilteredElementCollector(doc).OfClass(Grid).ToElements()
    view = doc.ActiveView

    for g in grids:

        # ---------------------------------------------------------
        # Try method 1: GetCurvesInView (works in most plan views)
        # ---------------------------------------------------------
        try:
            cvs = g.GetCurvesInView(DatumExtentType.ViewSpecific, view)
            if cvs:
                for c in cvs:
                    if isinstance(c, Curve):
                        segments.append(c)
                continue  # Successful, next grid
        except:
            pass

        # ---------------------------------------------------------
        # Method 2: fallback to geometry
        # ---------------------------------------------------------
        opts = Options()
        geom = g.get_Geometry(opts)

        if geom is None:
            continue

        for obj in geom:
            # Direct curve
            if isinstance(obj, Curve):
                segments.append(obj)

            # Segmented grids often hide inside GeometryInstance
            if isinstance(obj, GeometryInstance):
                try:
                    inst_geom = obj.GetInstanceGeometry()
                except:
                    inst_geom = None

                if inst_geom:
                    for sub in inst_geom:
                        if isinstance(sub, Curve):
                            segments.append(sub)

    return segments


# ---------------------------------------------------------
# Main algorithm
# ---------------------------------------------------------
sel = uidoc.Selection.GetElementIds()
if not sel:
    forms.alert("No walls selected.", exitscript=True)

walls = []
for id in sel:
    el = doc.GetElement(id)
    if isinstance(el, Wall):
        walls.append(el)

if not walls:
    forms.alert("Selection contains no walls.", exitscript=True)


# Pre-collect grid segments
grid_segments = get_all_grid_segments(doc)
if not grid_segments:
    forms.alert("No grid curves found in model.", exitscript=True)


with Transaction(doc, "Align Wall Orientation to Grids") as t:
    t.Start()

    aligned = 0
    skipped = 0

    for wall in walls:
        loc = wall.Location

        if not isinstance(loc, LocationCurve):
            skipped += 1
            continue

        curve = loc.Curve
        if not isinstance(curve, Line):
            skipped += 1
            continue  # Ignore curved walls

        # Wall direction
        wdir = curve_direction(curve)

        # Midpoint for rotation pivot
        p0 = curve.GetEndPoint(0)
        p1 = curve.GetEndPoint(1)
        mid = (p0 + p1) * 0.5

        # ---------------------------------------------------------
        # Find a valid grid (angle <= 3deg AND distance <= 30m)
        # ---------------------------------------------------------
        MAX_ANGLE = math.radians(3.0)          # 3 degrees
        MAX_DIST  = 30000.0 / 304.8            # 30m in feet

        best_dir = None
        best_dist = float('inf')

        for gcurve in grid_segments:
            if not isinstance(gcurve, Line):
                continue

            gdir = curve_direction(gcurve)

            # -----------------------------
            # 1. ANGLE CHECK (parallelism)
            # -----------------------------
            ang = abs(signed_angle(wdir, gdir))
            if ang > MAX_ANGLE:
                continue     # reject grid: not parallel enough

            # ---------------------------------------------
            # 2. DISTANCE CHECK (within 30m from midpoint)
            # ---------------------------------------------
            sp = gcurve.GetEndPoint(0)
            ep = gcurve.GetEndPoint(1)
            line = Line.CreateBound(sp, ep)

            proj = line.Project(mid)
            if proj is None:
                continue

            d = proj.Distance
            if d > MAX_DIST:
                continue     # reject grid: too far away

            # -------------------------------------
            # Candidate accepted: choose nearest
            # -------------------------------------
            if d < best_dist:
                best_dist = d
                best_dir = gdir

        # If no valid grid found, skip this wall
        if best_dir is None:
            skipped += 1
            continue

        nearest_dir = best_dir

        # ---------------------------------------------------------
        # Compute rotation angle
        # ---------------------------------------------------------
        angle = signed_angle(wdir, nearest_dir)

        # Skip tiny angles
        if abs(angle) < 1e-5:
            continue

        # Axis for rotation (vertical line through midpoint)
        axis = Line.CreateBound(mid, mid + XYZ(0, 0, 1))

        try:
            ElementTransformUtils.RotateElement(doc, wall.Id, axis, angle)
            aligned += 1
        except:
            skipped += 1

    t.Commit()

