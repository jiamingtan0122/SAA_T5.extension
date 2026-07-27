# -*- coding: utf-8 -*-
__title__ = "Wall\nAttach Check"
__author__ = "JM"
__doc__ = """Version = 2.0
Date    = 29.06.2026
_____________________________________________________________________
Description:

Checks all walls in the active view for Top/Base attachment and
highlights the ones that are NOT attached AND NOT physically touching
a floor/beam/roof above (or below for base).

Two checks are combined per wall:
    1. Parameter check  -> WALL_TOP_IS_ATTACHED / WALL_BOTTOM_IS_ATTACHED
                            (true "Attach Top/Base" command was used)
    2. Geometry check   -> wall's top/base elevation bounding-box
                            touches a Floor / Structural Framing (beam)
                            / Roof element within tolerance, in either
                            the HOST model or any LOADED linked model
                            (covers manually-adjusted Top/Base Offset
                            cases where Attach was never used)

A wall is only highlighted if BOTH checks fail.

Color scheme:
    RED    -> Top NOT attached and NOT touching anything above
    ORANGE -> Base NOT attached and NOT touching anything below
              (only flagged if top is OK)

Run again and choose "Clear Overrides" to remove overrides applied
by this script (clears on ALL walls in the model, not just the
currently visible/filtered ones, so it always succeeds).

Single transaction: "HighlightWallAttach" / "ClearWallAttachOverrides"
_____________________________________________________________________
"""

from pyrevit import revit, DB
from pyrevit.forms import SelectFromList, alert
from Autodesk.Revit.DB import (
    Transaction, FilteredElementCollector, BuiltInCategory,
    BuiltInParameter, OverrideGraphicSettings, Color,
    FillPatternElement, ElementId, RevitLinkInstance, XYZ,
    IFailuresPreprocessor, FailureProcessingResult
)
import sys
import traceback

# ---------------- Tolerances (adjust if needed) ----------------
# Vertical gap allowed between wall top/base and the touching
# floor/beam/roof to still count as "touching". ~15mm default.
Z_TOLERANCE_FT = 0.05
# Extra XY footprint buffer for overlap test (handles tiny float
# precision/snapping gaps). ~3mm default.
XY_BUFFER_FT = 0.01

CANDIDATE_CATEGORIES = [
    BuiltInCategory.OST_Floors,
    BuiltInCategory.OST_StructuralFraming,   # beams
    BuiltInCategory.OST_Roofs,
]

# ---------------- Failure preprocessor (auto-OK all warnings) ----------------
class AutoOKAllWarnings(IFailuresPreprocessor):
    def PreprocessFailures(self, fa):
        try:
            for fm in fa.GetFailureMessages():
                if fm.GetSeverity() == DB.FailureSeverity.Warning:
                    fa.DeleteWarning(fm)
        except:
            pass
        return FailureProcessingResult.Continue

# ---------------- Helpers (IronPython-safe) ----------------
def get_attach_flag(wall, bip):
    """Return True/False/None for a wall's Top/Base attachment flag."""
    try:
        param = wall.get_Parameter(bip)
        if param and param.HasValue:
            return param.AsInteger() == 1
    except:
        pass
    return None

def bbox_minmax(bbox):
    return (bbox.Min.X, bbox.Min.Y, bbox.Min.Z, bbox.Max.X, bbox.Max.Y, bbox.Max.Z)

def transform_bbox_minmax(bbox, transform):
    """Transform all 8 corners of a bbox and return the new AABB tuple."""
    mn, mx = bbox.Min, bbox.Max
    corners = [
        XYZ(mn.X, mn.Y, mn.Z), XYZ(mx.X, mn.Y, mn.Z),
        XYZ(mn.X, mx.Y, mn.Z), XYZ(mx.X, mx.Y, mn.Z),
        XYZ(mn.X, mn.Y, mx.Z), XYZ(mx.X, mn.Y, mx.Z),
        XYZ(mn.X, mx.Y, mx.Z), XYZ(mx.X, mx.Y, mx.Z),
    ]
    pts = [transform.OfPoint(c) for c in corners]
    xs = [p.X for p in pts]
    ys = [p.Y for p in pts]
    zs = [p.Z for p in pts]
    return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))

def xy_overlap(a, b, buffer):
    """AABB overlap test in X/Y only, with a small buffer tolerance."""
    return not (a[3] + buffer < b[0] or b[3] + buffer < a[0] or
                a[4] + buffer < b[1] or b[4] + buffer < a[1])

def gather_candidate_bboxes(rvt_doc):
    """Collect bounding boxes (as min/max tuples, in HOST coordinates)
    for Floors/Beams/Roofs from the host model and every loaded link."""
    candidates = []

    # Host model elements
    for cat in CANDIDATE_CATEGORIES:
        try:
            for el in FilteredElementCollector(rvt_doc).OfCategory(cat).WhereElementIsNotElementType():
                try:
                    bb = el.get_BoundingBox(None)
                    if bb:
                        candidates.append(bbox_minmax(bb))
                except:
                    pass
        except:
            pass

    # Linked model elements (transformed into host coordinates)
    try:
        for link in FilteredElementCollector(rvt_doc).OfClass(RevitLinkInstance):
            try:
                link_doc = link.GetLinkDocument()
                if not link_doc:
                    continue  # link unloaded
                transform = link.GetTotalTransform()
                for cat in CANDIDATE_CATEGORIES:
                    for el in FilteredElementCollector(link_doc).OfCategory(cat).WhereElementIsNotElementType():
                        try:
                            bb = el.get_BoundingBox(None)
                            if bb:
                                candidates.append(transform_bbox_minmax(bb, transform))
                        except:
                            pass
            except:
                pass
    except:
        pass

    return candidates

def touches_at_elevation(wall_bbox, candidates, target_z, look_above):
    """Check if any candidate touches the wall at target_z (wall top
    if look_above, wall base if not), with XY footprint overlap."""
    for c in candidates:
        if look_above:
            # candidate's bottom should sit near the wall's top
            if abs(c[2] - target_z) <= Z_TOLERANCE_FT:
                if xy_overlap(wall_bbox, c, XY_BUFFER_FT):
                    return True
        else:
            # candidate's top should sit near the wall's base
            if abs(c[5] - target_z) <= Z_TOLERANCE_FT:
                if xy_overlap(wall_bbox, c, XY_BUFFER_FT):
                    return True
    return False

def get_solid_fill_pattern_id(rvt_doc):
    """Prefer a DRAFTING-type solid fill pattern (renders consistently
    regardless of view scale). Falls back to any solid fill pattern."""
    drafting_solid = ElementId.InvalidElementId
    any_solid = ElementId.InvalidElementId
    try:
        for fp in FilteredElementCollector(rvt_doc).OfClass(FillPatternElement):
            pat = fp.GetFillPattern()
            if pat.IsSolidFill:
                any_solid = fp.Id
                if pat.Target == DB.FillPatternTarget.Drafting:
                    drafting_solid = fp.Id
                    break
    except:
        pass
    return drafting_solid if drafting_solid != ElementId.InvalidElementId else any_solid

def build_override(rgb, solid_fill_id):
    ogs = OverrideGraphicSettings()
    c = Color(rgb[0], rgb[1], rgb[2])

    if solid_fill_id != ElementId.InvalidElementId:
        ogs.SetCutForegroundPatternId(solid_fill_id)
        ogs.SetCutForegroundPatternColor(c)
        ogs.SetCutForegroundPatternVisible(True)
        ogs.SetSurfaceForegroundPatternId(solid_fill_id)
        ogs.SetSurfaceForegroundPatternColor(c)
        ogs.SetSurfaceForegroundPatternVisible(True)

    ogs.SetCutLineColor(c)
    ogs.SetCutLineWeight(6)
    ogs.SetProjectionLineColor(c)
    ogs.SetProjectionLineWeight(6)
    return ogs

# ---------------- MAIN ----------------
doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
view = doc.ActiveView

RED = (255, 0, 0)        # top not attached / not touching above
ORANGE = (255, 140, 0)   # base not attached / not touching below

mode = SelectFromList.show(
    ["Highlight Not-Attached Walls", "Clear Overrides (this view)"],
    title="Wall Attach Check",
    button_name="Run"
)
if not mode:
    sys.exit()

# ---------------- Single Transaction ----------------
if mode == "Clear Overrides (this view)":
    t = Transaction(doc, "ClearWallAttachOverrides")
else:
    t = Transaction(doc, "HighlightWallAttach")

t.Start()

fh = t.GetFailureHandlingOptions()
fh.SetFailuresPreprocessor(AutoOKAllWarnings())
t.SetFailureHandlingOptions(fh)

try:
    if mode == "Clear Overrides (this view)":
        # Document-wide collector (NOT view-scoped) so hidden/filtered/
        # temporarily-isolated walls still get cleared. SetElementOverrides
        # doesn't require the element to currently pass view visibility.
        all_walls = FilteredElementCollector(doc) \
            .OfCategory(BuiltInCategory.OST_Walls) \
            .WhereElementIsNotElementType()

        blank_ogs = OverrideGraphicSettings()
        count = 0
        for w in all_walls:
            view.SetElementOverrides(w.Id, blank_ogs)
            count += 1
        t.Commit()
        alert("Cleared overrides on {} wall(s) in view '{}'.".format(count, view.Name))
        sys.exit()

    # ---- Highlight mode ----
    collector = FilteredElementCollector(doc, view.Id) \
        .OfCategory(BuiltInCategory.OST_Walls) \
        .WhereElementIsNotElementType()
    walls = [w for w in collector if isinstance(w, DB.Wall)]

    if not walls:
        t.RollBack()
        alert("No walls found in the active view.")
        sys.exit()

    solid_fill_id = get_solid_fill_pattern_id(doc)
    candidates = gather_candidate_bboxes(doc)

    top_not_attached = []
    base_not_attached = []

    for w in walls:
        top_attached = get_attach_flag(w, BuiltInParameter.WALL_TOP_IS_ATTACHED)
        base_attached = get_attach_flag(w, BuiltInParameter.WALL_BOTTOM_IS_ATTACHED)

        wall_bbox = None
        try:
            bb = w.get_BoundingBox(None)
            if bb:
                wall_bbox = bbox_minmax(bb)
        except:
            pass

        # Geometry fallback: physically touching, even without "Attach"
        top_touching = False
        base_touching = False
        if wall_bbox and candidates:
            top_touching = touches_at_elevation(wall_bbox, candidates, wall_bbox[5], True)
            base_touching = touches_at_elevation(wall_bbox, candidates, wall_bbox[2], False)

        top_ok = (top_attached is True) or top_touching
        base_ok = (base_attached is True) or base_touching

        rgb = None
        if not top_ok:
            rgb = RED
            top_not_attached.append(w.Id)
        elif not base_ok:
            rgb = ORANGE
            base_not_attached.append(w.Id)

        if rgb:
            ogs = build_override(rgb, solid_fill_id)
            view.SetElementOverrides(w.Id, ogs)

    t.Commit()

    if top_not_attached or base_not_attached:
        alert(
            "Highlight applied in view '{}'.\n\n"
            "RED (top not attached / not touching above): {}\n"
            "ORANGE (base not attached / not touching below): {}".format(
                view.Name, len(top_not_attached), len(base_not_attached)
            )
        )
    else:
        alert("All walls in this view are attached or touching top & base. Nothing highlighted.")

except Exception as ex:
    if t.HasStarted():
        t.RollBack()
    alert("Script failed and changes were rolled back.\n\nError:\n{}".format(
        traceback.format_exc()
    ))