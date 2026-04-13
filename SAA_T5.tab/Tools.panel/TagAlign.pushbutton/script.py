# -*- coding: utf-8 -*-
__title__ = "Align\nTags"
__author__ = "JM"
__doc__ = """Version = 4.3
Date    = 13.04.2025
_____________________________________________________________________
Description:

Select any tags in the view (room tags, area tags, door tags,
equipment tags, etc.) then run this script.

Each selected tag is rotated so its text aligns parallel to the
longest STRAIGHT wall segment of its host room, relative to the
CURRENT VIEW ROTATION.

IMPORTANT: You MUST pre-select tags before running. The script
will not process anything if no elements are selected, to
prevent accidental bulk operations.
_____________________________________________________________________
"""

from pyrevit import revit, DB
from pyrevit.forms import alert
from Autodesk.Revit.DB import (
    FilteredElementCollector,
    SpatialElementBoundaryOptions,
    ElementTransformUtils,
    IndependentTag,
    Line, XYZ,
    FailureSeverity,
    FailureProcessingResult,
    BuiltInCategory,
)
import sys
import math

# -----------------------------------------------------------------------
# Failure suppressor
# -----------------------------------------------------------------------
class AutoOKWarnings(DB.IFailuresPreprocessor):
    def PreprocessFailures(self, fa):
        try:
            for fm in fa.GetFailureMessages():
                if fm.GetSeverity() == FailureSeverity.Warning:
                    fa.DeleteWarning(fm)
        except:
            pass
        return FailureProcessingResult.Continue


# -----------------------------------------------------------------------
# View rotation
# -----------------------------------------------------------------------
def get_view_rotation(view):
    try:
        rd = view.RightDirection
        return math.atan2(rd.Y, rd.X)
    except:
        return 0.0


# -----------------------------------------------------------------------
# Angle helpers
# -----------------------------------------------------------------------
def line_angle_in_view(curve, view_rotation):
    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    model_angle = math.atan2(p1.Y - p0.Y, p1.X - p0.X)
    view_angle  = model_angle - view_rotation
    if   view_angle >  math.pi / 2: view_angle -= math.pi
    elif view_angle < -math.pi / 2: view_angle += math.pi
    return view_angle


def longest_straight_wall_angle(spatial_elem, view_rotation):
    opts = SpatialElementBoundaryOptions()
    try:
        loops = spatial_elem.GetBoundarySegments(opts)
    except:
        return None
    longest = None
    max_len = 0.0
    for loop in loops:
        for seg in loop:
            c = seg.GetCurve()
            if not isinstance(c, Line):
                continue
            if c.Length > max_len:
                max_len = c.Length
                longest = c
    if longest is None:
        return None
    return line_angle_in_view(longest, view_rotation)


# -----------------------------------------------------------------------
# Polygon helpers
# -----------------------------------------------------------------------
def point_in_polygon(pt_x, pt_y, curves):
    inside = False
    for c in curves:
        x0, y0 = c.GetEndPoint(0).X, c.GetEndPoint(0).Y
        x1, y1 = c.GetEndPoint(1).X, c.GetEndPoint(1).Y
        if (y0 > pt_y) != (y1 > pt_y):
            x_int = x0 + (pt_y - y0) * (x1 - x0) / (y1 - y0)
            if pt_x < x_int:
                inside = not inside
    return inside


def boundary_curves(spatial_elem):
    opts = SpatialElementBoundaryOptions()
    try:
        loops = spatial_elem.GetBoundarySegments(opts)
    except:
        return []
    return [seg.GetCurve() for loop in loops for seg in loop]


def bounding_box_xy(curves):
    xs = [c.GetEndPoint(i).X for c in curves for i in (0, 1)]
    ys = [c.GetEndPoint(i).Y for c in curves for i in (0, 1)]
    return min(xs), min(ys), max(xs), max(ys)


def centroid_xy(curves):
    pts  = []
    seen = set()
    for c in curves:
        for i in (0, 1):
            p   = c.GetEndPoint(i)
            key = (round(p.X, 6), round(p.Y, 6))
            if key not in seen:
                seen.add(key)
                pts.append((p.X, p.Y))
    if not pts:
        return None, None
    return (sum(x for x, _ in pts) / len(pts),
            sum(y for _, y in pts) / len(pts))


def nearest_interior_point(cx, cy, curves, grid_steps=12):
    if point_in_polygon(cx, cy, curves):
        return cx, cy
    min_x, min_y, max_x, max_y = bounding_box_xy(curves)
    step_x = (max_x - min_x) / grid_steps
    step_y = (max_y - min_y) / grid_steps
    if step_x < 1e-6 or step_y < 1e-6:
        return cx, cy
    best_x, best_y = cx, cy
    best_dist      = float('inf')
    for i in range(1, grid_steps):
        for j in range(1, grid_steps):
            sx = min_x + i * step_x
            sy = min_y + j * step_y
            if not point_in_polygon(sx, sy, curves):
                continue
            dist = math.hypot(sx - cx, sy - cy)
            if dist < best_dist:
                best_dist      = dist
                best_x, best_y = sx, sy
    return best_x, best_y


# -----------------------------------------------------------------------
# Move room reference point to interior centroid
# -----------------------------------------------------------------------
def center_room_reference_point(rvt_doc, room, curves):
    loc = room.Location
    if loc is None:
        return False
    current_pt = loc.Point
    cx, cy = centroid_xy(curves)
    if cx is None:
        return False
    tx, ty     = nearest_interior_point(cx, cy, curves)
    dist       = math.hypot(tx - current_pt.X, ty - current_pt.Y)
    if dist < 0.003:
        return False
    try:
        loc.Move(XYZ(tx - current_pt.X, ty - current_pt.Y, 0))
        return True
    except:
        return False


# -----------------------------------------------------------------------
# Room data cache
# -----------------------------------------------------------------------
def build_room_data(rvt_doc, view, view_rotation):
    rooms = (FilteredElementCollector(rvt_doc, view.Id)
             .OfCategory(BuiltInCategory.OST_Rooms)
             .WhereElementIsNotElementType()
             .ToElements())
    data = {}
    for room in rooms:
        if room.Area <= 0:
            continue
        curves = boundary_curves(room)
        if not curves:
            continue
        angle = longest_straight_wall_angle(room, view_rotation)
        if angle is None:
            continue
        data[room.Id.IntegerValue] = {
            'room'  : room,
            'curves': curves,
            'angle' : angle,
        }
    return data


def build_pip_list(room_data):
    return [(v['curves'], v['angle']) for v in room_data.values()]


def angle_from_pip(pt, pip_list):
    for curves, angle in pip_list:
        if point_in_polygon(pt.X, pt.Y, curves):
            return angle
    return None


# -----------------------------------------------------------------------
# Rotate tag
# -----------------------------------------------------------------------
def rotate_tag_absolute(rvt_doc, tag, target_angle):
    loc = tag.Location
    if loc is None:
        return False
    tag_pt = loc.Point
    axis   = Line.CreateBound(tag_pt, XYZ(tag_pt.X, tag_pt.Y, tag_pt.Z + 1.0))
    try:
        tf      = tag.GetTransform()
        bx      = tf.BasisX
        current = math.atan2(bx.Y, bx.X)
    except:
        try:
            current = loc.Rotation
        except:
            current = 0.0
    delta = target_angle - current
    delta = (delta + math.pi) % (2 * math.pi) - math.pi
    if abs(delta) < 1e-4:
        return True
    ElementTransformUtils.RotateElement(rvt_doc, tag.Id, axis, delta)
    return True


# -----------------------------------------------------------------------
# Collect and validate selected tags — NO fallback default
# -----------------------------------------------------------------------
def collect_selected_tags(rvt_doc, sel_ids):
    """
    Filters the user's current selection down to tag elements only.
    Returns an empty list if nothing tag-like is selected —
    the caller must handle the empty case and abort.
    """
    SPATIAL_TAG_CATS = {
        int(BuiltInCategory.OST_RoomTags),
        int(BuiltInCategory.OST_AreaTags),
    }
    tags = []
    for eid in sel_ids:
        elem = rvt_doc.GetElement(eid)
        if elem is None:
            continue
        if isinstance(elem, IndependentTag):
            tags.append(elem)
            continue
        cat = elem.Category
        if cat and cat.Id.IntegerValue in SPATIAL_TAG_CATS:
            tags.append(elem)
    return tags


def get_target_angle(tag, room_data, pip_list, view_rotation):
    try:
        room = tag.Room
        if room is not None:
            key = room.Id.IntegerValue
            if key in room_data:
                return room_data[key]['angle']
            return longest_straight_wall_angle(room, view_rotation)
    except:
        pass
    try:
        area = tag.Area
        if area is not None:
            return longest_straight_wall_angle(area, view_rotation)
    except:
        pass
    try:
        pt = tag.TagHeadPosition
        if pt is not None:
            return angle_from_pip(pt, pip_list)
    except:
        pass
    try:
        loc = tag.Location
        if loc:
            return angle_from_pip(loc.Point, pip_list)
    except:
        pass
    return None


# -----------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------
doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
view  = doc.ActiveView

# ---- Require explicit selection — no default fallback -----------------
sel_ids = list(uidoc.Selection.GetElementIds())

if not sel_ids:
    alert(
        "Nothing selected.\n\n"
        "Please select one or more tags in the view first, then run this tool.",
        title="Align Tags — No Selection"
    )
    sys.exit()

tags = collect_selected_tags(doc, sel_ids)

if not tags:
    alert(
        "No tags found in the current selection.\n\n"
        "Select room tags, area tags, or any annotation tags, then run again.",
        title="Align Tags — No Tags Selected"
    )
    sys.exit()

# ---- Build room data --------------------------------------------------
view_rotation = get_view_rotation(view)
room_data     = build_room_data(doc, view, view_rotation)

if not room_data:
    alert(
        "No placed rooms with straight walls found in the active view.\n\n"
        "Ensure the active view contains placed rooms.",
        title="Align Tags — No Rooms"
    )
    sys.exit()

pip_list = build_pip_list(room_data)

moved   = 0
rotated = 0
skipped = 0
no_room = 0

# -----------------------------------------------------------------------
t = DB.Transaction(doc, "AlignTags")
t.Start()

fh = t.GetFailureHandlingOptions()
fh.SetFailuresPreprocessor(AutoOKWarnings())
t.SetFailureHandlingOptions(fh)

try:
    # Step 1: move room reference points to interior centroid
    for entry in room_data.values():
        if center_room_reference_point(doc, entry['room'], entry['curves']):
            moved += 1

    # Step 2: rotate selected tags
    for tag in tags:
        angle = get_target_angle(tag, room_data, pip_list, view_rotation)
        if angle is None:
            no_room += 1
            skipped += 1
            continue
        if rotate_tag_absolute(doc, tag, angle):
            rotated += 1
        else:
            skipped += 1

    t.Commit()

except Exception as ex:
    if t.HasStarted():
        t.RollBack()
    alert("Error: {}".format(ex), title="Align Tags — Error")
    sys.exit()

print("Done.  Ref points moved: {}  |  Tags rotated: {}  |  "
      "Skipped: {}  |  No room found: {}".format(
      moved, rotated, skipped, no_room))