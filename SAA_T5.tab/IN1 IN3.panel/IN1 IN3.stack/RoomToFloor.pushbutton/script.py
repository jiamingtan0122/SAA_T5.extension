# -*- coding: utf-8 -*-
__title__ = "Room To\nFinish"
__author__ = "JM"
__doc__ = """Version = 1.16
Date    = 24.08.2026
Description:
Generates Floor Finishes from selected Rooms, using each Room's
'Floor Finish' parameter to resolve which Floor Type to use.

- Step 1: Click the button. You'll be prompted twice:
    a) Pick Room(s) in the CURRENT model (click Finish, or Cancel to skip).
    b) Pick Room(s) in LINKED model(s) (click Finish, or Cancel to skip).
  (Split into two passes because Revit's API can only pick into a link
  via a dedicated linked-element pick mode - it can't mix host and
  linked picking in a single pass.)
- Step 2: Enter Height Offset (mm) - default 0mm.
- Logic:
    - A Room is SKIPPED if:
        - it is unbound/unplaced (Area = 0)
        - 'FOH BOH' parameter contains "FOH"
        - 'GIFA Key' parameter contains "NON GIFA"
        - 'GIFA Key' parameter contains "TOILET"
    - Reads 'Floor Finish' text parameter on each remaining Room
      (e.g. "CPT-01").
    - Finds Floor Types whose name starts with that code.
        - 1 match  -> used automatically.
        - >1 match -> asks once per unique code, then reuses the choice
                      for every other room sharing that code.
        - 0 match  -> room is skipped and reported at the end.
    - Boundary geometry tries three tiers in order: NATIVE (Revit's own
      segments, zero processing - matches proven community Room-to-Floor
      tooling), then SNAPPED (native segments with only genuinely small
      gaps individually corrected, e.g. at a movement joint between two
      linked models - everything else, including arcs, stays untouched),
      then TESSELLATED-FALLBACK (full point reconstruction) only as a last
      resort. The fallback's floor is SKIPPED entirely (not created) if
      its area disagrees with the Room's own Area by more than 15% - a
      distorted automatic floor is worse than none. If ALL THREE tiers
      fail at the normal 'Finish' boundary location (this always happens
      for a Curtain Wall side, since curtain walls have no layers to
      compute a finish face from), the whole pipeline is retried at
      'Center' location, which works for any wall type. The Result
      column shows which tier/location succeeded for every created floor
      (' @Center' suffix marks a Center-location result).
    - Target Level = the Room's own Level.
    - Floor is created as STRUCTURAL (Structural checkbox ticked). Confirmed
      after creation by checking/forcing the 'Structural' parameter.
    - Writes the Room Name into the Floor's 'SAA_Floor-Room' parameter
      (if that parameter exists on the Floor).
    - Every selected room (skipped or created) is printed in ONE table
      in the output window, with its raw parameter values and outcome,
      so 'no floors created' always comes with a reason per room.
    - Each created Floor's actual area is compared to the Room's own
      Area parameter; a >5% mismatch is flagged inline in the Result
      column (usually means a room-bounding wall - often in a linked
      model - wasn't recognized, so the Room/floor shape bled past it).
"""

from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, ElementId, RevitLinkInstance,
    SpatialElement, SpatialElementBoundaryOptions,
    SpatialElementBoundaryLocation, FilteredElementCollector,
    Floor, FloorType, Level, Transaction, CurveLoop, Line, Arc, XYZ,
    GeometryCreationUtilities, PlanarFace, IFailuresPreprocessor,
    FailureProcessingResult, FailureSeverity
)
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from Autodesk.Revit.Exceptions import OperationCanceledException
from pyrevit import revit, forms, script
import traceback

import clr
clr.AddReference("System")
from System.Collections.Generic import List

# ---------------- INITIALIZATION ----------------
doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

FLOOR_FINISH_PARAM_NAME = "Floor Finish"
SAA_FLOOR_ROOM_PARAM_NAME = "SAA_Floor-Room"
FOH_BOH_PARAM_NAME = "FOH BOH"
GIFA_KEY_PARAM_NAME = "GIFA Key"


def safe_str(val):
    """Safely coerce a value (possibly unicode) to a printable str."""
    try:
        return str(val)
    except Exception:
        try:
            return val.encode("utf-8", "replace")
        except Exception:
            return "<unprintable>"


class AutoOkFailurePreprocessor(IFailuresPreprocessor):
    """Auto-accepts resolvable warnings so batch floor creation doesn't stall on dialogs."""
    def PreprocessFailures(self, failures_accessor):
        failures = failures_accessor.GetFailureMessages()
        for f in failures:
            if f.GetSeverity() == FailureSeverity.Warning:
                failures_accessor.DeleteWarning(f)
        return FailureProcessingResult.Continue


class RoomSelectionFilter(ISelectionFilter):
    """Restricts picking to Room elements - host model directly, or inside links."""
    def AllowElement(self, elem):
        if isinstance(elem, RevitLinkInstance):
            return True
        try:
            return (elem.Category is not None and
                    elem.Category.Id.IntegerValue == int(BuiltInCategory.OST_Rooms))
        except Exception:
            return False

    def AllowReference(self, reference, position):
        # Called for elements resolved via reference, including linked elements.
        if reference.LinkedElementId != ElementId.InvalidElementId:
            link_inst = doc.GetElement(reference.ElementId)
            if isinstance(link_inst, RevitLinkInstance):
                link_doc = link_inst.GetLinkDocument()
                if link_doc:
                    linked_elem = link_doc.GetElement(reference.LinkedElementId)
                    return isinstance(linked_elem, SpatialElement)
            return False
        return True

# ---------------- HELPERS ----------------

def flatten_pt(pt):
    """Forces a point to exactly Z=0."""
    return XYZ(pt.X, pt.Y, 0.0)


def get_type_name_safe(element):
    param = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
    if param and param.HasValue:
        return param.AsString()
    try:
        return element.Name
    except Exception:
        return "Unnamed Type"


def get_all_floor_types():
    return FilteredElementCollector(doc).OfClass(FloorType).WhereElementIsElementType().ToElements()


def get_room_number(room):
    param = room.get_Parameter(BuiltInParameter.ROOM_NUMBER)
    if param and param.HasValue:
        val = param.AsString()
        return val.strip() if val else None
    return None


def get_room_param_string(room, param_name):
    """Generic reader for a text/string Room parameter by name. Returns None if blank/missing."""
    param = room.LookupParameter(param_name)
    if not param or not param.HasValue:
        return None
    try:
        val = param.AsString()
    except Exception:
        val = None
    if not val:
        try:
            val = param.AsValueString()
        except Exception:
            val = None
    return val.strip() if val else None


def get_room_finish_code(room):
    """Reads the 'Floor Finish' text parameter from a Room. Returns None if blank/missing."""
    return get_room_param_string(room, FLOOR_FINISH_PARAM_NAME)


def find_matching_floor_types(code, floor_types_dict):
    """Returns list of (name, FloorType) whose name starts with the code (case-insensitive)."""
    code_lower = code.lower()
    matches = [(name, ft) for name, ft in floor_types_dict.items()
               if name.lower().startswith(code_lower)]
    # Fallback: if nothing starts with it, try "contains" as a looser pass
    if not matches:
        matches = [(name, ft) for name, ft in floor_types_dict.items()
                   if code_lower in name.lower()]
    return sorted(matches, key=lambda x: x[0])


def get_room_curve_loops_native(room, transform=None, boundary_location=None):
    """
    PRIMARY METHOD. Builds CurveLoops directly from Revit's own room boundary
    segments with NO manual flattening or point reconstruction - just
    seg.GetCurve() per segment, matching the approach used by proven
    community Room-to-Floor tools. Each Revit-reported loop (outer boundary
    + any islands) becomes its own CurveLoop; Floor.Create handles the set
    natively. Returns a .NET List[CurveLoop], or None if there are no
    boundary segments. Raises if a segment curve can't be transformed/appended.
    """
    options = SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = boundary_location or SpatialElementBoundaryLocation.Finish

    segments_list = room.GetBoundarySegments(options)
    if not segments_list:
        return None

    curve_loops = List[CurveLoop]()
    for room_outline in segments_list:
        curve_loop = CurveLoop()
        for seg in room_outline:
            curve = seg.GetCurve()
            if transform:
                curve = curve.CreateTransformed(transform)
            curve_loop.Append(curve)
        curve_loops.Add(curve_loop)

    return curve_loops


def get_room_curve_loops_snapped(room, transform=None, snap_tol=0.15, boundary_location=None):
    """
    SECOND TIER - used only if the native method's loop is rejected as
    discontinuous. Walks the native segments in order and, wherever two
    CONSECUTIVE segments have a small real gap (up to ~45mm, e.g. from a
    room whose boundary crosses into a different linked model with a small
    real coordination offset, or slightly different precision), nudges
    just that one segment's start point to close the gap - preserving Arc
    curvature via 3-point reconstruction where possible. Every other
    segment is left completely untouched at full native fidelity, unlike
    the full-tessellation fallback which can distort long/complex
    boundaries by aggressively snapping unrelated points together.
    Raises if any gap is larger than snap_tol - at that size it's more
    likely a genuinely missing boundary segment (e.g. a linked wall not
    currently recognized as room-bounding) than a precision artifact, and
    should not be silently patched over.
    """
    options = SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = boundary_location or SpatialElementBoundaryLocation.Finish
    segments_list = room.GetBoundarySegments(options)
    if not segments_list:
        return None

    curve_loops = List[CurveLoop]()
    for room_outline in segments_list:
        curves_in_loop = []
        for seg in room_outline:
            curve = seg.GetCurve()
            if transform:
                curve = curve.CreateTransformed(transform)
            curves_in_loop.append(curve)

        if not curves_in_loop:
            continue

        corrected = []
        prev_end = None
        for curve in curves_in_loop:
            start = curve.GetEndPoint(0)
            end = curve.GetEndPoint(1)
            if prev_end is not None:
                gap = prev_end.DistanceTo(start)
                if 1e-9 < gap <= snap_tol:
                    if isinstance(curve, Arc):
                        try:
                            mid = curve.Evaluate(0.5, True)
                            curve = Arc.Create(prev_end, end, mid)
                        except Exception:
                            curve = Line.CreateBound(prev_end, end)
                    else:
                        curve = Line.CreateBound(prev_end, end)
                elif gap > snap_tol:
                    raise ValueError("Gap too large to snap safely: {:.1f}mm".format(gap * 304.8))
            corrected.append(curve)
            prev_end = curve.GetEndPoint(1)

        # Close the loop the same way, if needed
        first_start = corrected[0].GetEndPoint(0)
        closing_gap = prev_end.DistanceTo(first_start)
        if 1e-9 < closing_gap <= snap_tol:
            last_curve = corrected[-1]
            if isinstance(last_curve, Arc):
                try:
                    mid = last_curve.Evaluate(0.5, True)
                    corrected[-1] = Arc.Create(last_curve.GetEndPoint(0), first_start, mid)
                except Exception:
                    corrected[-1] = Line.CreateBound(last_curve.GetEndPoint(0), first_start)
            else:
                corrected[-1] = Line.CreateBound(last_curve.GetEndPoint(0), first_start)
        elif closing_gap > snap_tol:
            raise ValueError("Closing gap too large to snap safely: {:.1f}mm".format(closing_gap * 304.8))

        curve_loop = CurveLoop()
        for c in corrected:
            curve_loop.Append(c)
        curve_loops.Add(curve_loop)

    return curve_loops


def get_room_boundary_solid_robust(room, transform=None, boundary_location=None):
    """
    FALLBACK ONLY - builds each loop by TESSELLATING every boundary segment
    into points and explicitly chaining them (reusing the same point object
    at every shared junction, including the loop closure). This tolerates
    precision drift between segments from different owning documents that
    would otherwise make CurveLoop.Append() reject the connection ("loop
    discontinuous"), but on complex/winding boundaries the point-snapping
    can occasionally join the wrong points and distort the shape - so it is
    only used when the native method fails outright.
    """
    options = SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = boundary_location or SpatialElementBoundaryLocation.Finish

    loops = []

    segments_list = room.GetBoundarySegments(options)
    if not segments_list:
        return None

    JOIN_TOL = 0.01     # feet (~3mm) - snap tolerance between adjacent segment endpoints
    DEGEN_TOL = 0.0007  # feet (~0.2mm) - drop points closer together than this

    for segments in segments_list:
        pts = []
        for seg in segments:
            curve = seg.GetCurve()
            if transform:
                curve = curve.CreateTransformed(transform)

            tess_pts = None
            try:
                raw = curve.Tessellate()
                if raw and len(list(raw)) >= 2:
                    tess_pts = [flatten_pt(p) for p in raw]
            except Exception:
                tess_pts = None

            if not tess_pts:
                # Tessellate() failed or returned too few points (seen on some
                # curtain wall / mullion-bound segments) - fall back to the
                # curve's own endpoints so this segment still contributes.
                p0 = flatten_pt(curve.GetEndPoint(0))
                p1 = flatten_pt(curve.GetEndPoint(1))
                if p0.DistanceTo(p1) < DEGEN_TOL:
                    continue  # genuinely zero-length segment - skip it only
                tess_pts = [p0, p1]

            if pts and pts[-1].DistanceTo(tess_pts[0]) < JOIN_TOL:
                # Shared junction with the previous segment - drop the duplicate,
                # the previous point object is reused below for exact continuity.
                tess_pts = tess_pts[1:]
            pts.extend(tess_pts)

        # Collapse near-duplicate consecutive points (degenerate segments)
        cleaned_pts = []
        for p in pts:
            if not cleaned_pts or cleaned_pts[-1].DistanceTo(p) > DEGEN_TOL:
                cleaned_pts.append(p)

        if len(cleaned_pts) < 4:
            raise ValueError("only {}pts/{}segs".format(len(cleaned_pts), len(segments)))

        # Force the closing point to be the exact same object as the start point,
        # guaranteeing the last segment connects with zero tolerance issues.
        if cleaned_pts[0].DistanceTo(cleaned_pts[-1]) > DEGEN_TOL:
            cleaned_pts.append(cleaned_pts[0])
        else:
            cleaned_pts[-1] = cleaned_pts[0]

        curve_loop = CurveLoop()
        for i in range(len(cleaned_pts) - 1):
            curve_loop.Append(Line.CreateBound(cleaned_pts[i], cleaned_pts[i + 1]))

        loops.append(curve_loop)

    if not loops:
        return None

    return GeometryCreationUtilities.CreateExtrusionGeometry(loops, XYZ.BasisZ, 1.0)


def _try_boundary_tiers(doc, room, transform, floor_type_id, level_id, room_area_sqft, boundary_location, location_label):
    """
    Runs the three-tier boundary construction pipeline (native / snapped /
    tessellated-fallback) at a single SpatialElementBoundaryLocation.
    Returns (floor_or_None, method_label).
    """
    for builder, label in (
        (get_room_curve_loops_native, "native"),
        (get_room_curve_loops_snapped, "snapped"),
    ):
        try:
            curve_loops = builder(room, transform, boundary_location=boundary_location)
        except Exception:
            continue
        if curve_loops is not None and curve_loops.Count > 0:
            try:
                return Floor.Create(doc, curve_loops, floor_type_id, level_id), label + location_label
            except Exception:
                continue

    try:
        fallback_solid = get_room_boundary_solid_robust(room, transform, boundary_location=boundary_location)
        fallback_face = get_bottom_face(fallback_solid)
    except Exception as ex:
        return None, "fail" + location_label + ":" + safe_str(ex)

    if not fallback_face:
        return None, "fail" + location_label + ":no boundary"

    if room_area_sqft > 0:
        diff_pct = abs(fallback_face.Area - room_area_sqft) / room_area_sqft * 100.0
        if diff_pct > 15.0:
            return None, "fail{}:area {:.0f}% off (check Room Bounding)".format(location_label, diff_pct)

    fallback_loops = fallback_face.GetEdgesAsCurveLoops()
    return Floor.Create(doc, fallback_loops, floor_type_id, level_id), "tessellated-fallback" + location_label


def create_floor_from_room(doc, room, transform, floor_type_id, level_id, room_area_sqft=0.0):
    """
    Creates the Floor for a room. Tries the three-tier pipeline (native /
    snapped / tessellated-fallback - see _try_boundary_tiers) first at
    SpatialElementBoundaryLocation.Finish, which is the most accurate but
    requires walls to have Core/Finish layers - a plain Curtain Wall has NO
    layers at all, so Revit silently OMITS that side's segment entirely
    when queried at Finish location (this is a Revit API limitation, not a
    bug in this script). If every tier fails at Finish location, the whole
    pipeline is retried at .Center location instead, which works for any
    wall type including curtain walls (at the cost of the floor edge
    sitting on that wall's centerline rather than its finish face on that
    one side).

    Returns (floor_or_None, method_label). method_label ends with
    ' @Center' if the Center-location retry was what succeeded.
    """
    floor, method = _try_boundary_tiers(
        doc, room, transform, floor_type_id, level_id, room_area_sqft,
        SpatialElementBoundaryLocation.Finish, ""
    )
    if floor:
        return floor, method

    floor, method_center = _try_boundary_tiers(
        doc, room, transform, floor_type_id, level_id, room_area_sqft,
        SpatialElementBoundaryLocation.Center, " @Center"
    )
    if floor:
        return floor, method_center

    return None, "F[" + method + "] C[" + method_center + "]"


def count_boundary_loops(room):
    """Returns the number of boundary loops Revit reports for this room
    (1 = simple room, >1 = the room has islands - interior columns/walls
    poking into it - which can distort the resulting floor shape)."""
    try:
        options = SpatialElementBoundaryOptions()
        options.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Finish
        segments_list = room.GetBoundarySegments(options)
        return len(segments_list) if segments_list else 0
    except Exception:
        return -1


def get_bottom_face(solid):
    """Returns the downward-facing PlanarFace of a solid (the floor's footprint face), or None."""
    if not solid:
        return None
    for face in solid.Faces:
        if isinstance(face, PlanarFace):
            if face.FaceNormal.IsAlmostEqualTo(XYZ(0, 0, -1)):
                return face
    return None


def ensure_structural(floor):
    """
    Confirms the created Floor is structural (Structural checkbox = Yes).
    Returns a short status string for reporting: 'OK', 'Forced to Yes', or
    'Param not found'.
    """
    param = floor.get_Parameter(BuiltInParameter.FLOOR_PARAM_IS_STRUCTURAL)
    if not param:
        return "Param not found"
    try:
        current = param.AsInteger()
    except Exception:
        return "Param not found"
    if current == 1:
        return "OK"
    if param.IsReadOnly:
        return "Still Non-Structural (read-only)"
    try:
        param.Set(1)
        return "Forced to Yes"
    except Exception:
        return "Still Non-Structural (could not set)"


def get_room_area_sqft(room):
    """Returns the Room's Area in internal units (sq ft). 0.0 if unplaced/unbound."""
    param = room.get_Parameter(BuiltInParameter.ROOM_AREA)
    if param and param.HasValue:
        try:
            return param.AsDouble()
        except Exception:
            return 0.0
    return 0.0


def sqft_to_sqm(val):
    return val * 0.09290304


def set_saa_floor_room_param(floor, room_name_value):
    """Writes the Room Name into the Floor's 'SAA_Floor-Room' parameter if it exists."""
    param = floor.LookupParameter(SAA_FLOOR_ROOM_PARAM_NAME)
    if not param:
        return "Param not found"
    if param.IsReadOnly:
        return "Read-only"
    try:
        param.Set(room_name_value if room_name_value else "")
        return "OK"
    except Exception:
        return "Could not set"


# ---------------- MAIN LOGIC ----------------

output.print_md("## Room Finish - Run Log")
output.print_md("_Engine check: v1.16, Center-location fallback ACTIVE_")

try:
    host_refs = uidoc.Selection.PickObjects(
        ObjectType.Element,
        RoomSelectionFilter(),
        "Select Room(s) in the CURRENT model, then click Finish (or press Esc/Cancel to skip)"
    )
except OperationCanceledException:
    host_refs = []

try:
    linked_refs = uidoc.Selection.PickObjects(
        ObjectType.LinkedElement,
        RoomSelectionFilter(),
        "Select Room(s) in LINKED model(s), then click Finish (or press Esc/Cancel to skip)"
    )
except OperationCanceledException:
    linked_refs = []

sel_refs = list(host_refs) + list(linked_refs)

if not sel_refs:
    forms.alert("No rooms selected.", exitscript=True)

all_floor_types = get_all_floor_types()
floor_types_dict = {get_type_name_safe(ft): ft for ft in all_floor_types if get_type_name_safe(ft)}
if not floor_types_dict:
    forms.alert("No Floor Types found in the project.", exitscript=True)

offset_str = forms.ask_for_string(
    default="0",
    prompt="Enter Height Offset (mm):",
    title="Height Offset"
)
if not offset_str:
    script.exit()
try:
    height_offset_feet = float(offset_str) / 304.8
except Exception:
    forms.alert("Invalid Number.", exitscript=True)

# --- PROCESSING ---

code_to_type_cache = {}   # code -> FloorType (resolved once per unique code per run)
diagnostic_log = []       # one row per selected room, always populated
floors_created = 0

t = Transaction(doc, "Create Floor Finishes")
fail_opts = t.GetFailureHandlingOptions()
fail_opts.SetFailuresPreprocessor(AutoOkFailurePreprocessor())
t.SetFailureHandlingOptions(fail_opts)
t.Start()

for ref in sel_refs:
    room_element = None
    transform = None
    target_level_id = None

    if ref.LinkedElementId != ElementId.InvalidElementId:
        link_inst = doc.GetElement(ref.ElementId)
        if isinstance(link_inst, RevitLinkInstance):
            link_doc = link_inst.GetLinkDocument()
            if link_doc:
                room_element = link_doc.GetElement(ref.LinkedElementId)
                transform = link_inst.GetTotalTransform()
    else:
        room_element = doc.GetElement(ref.ElementId)

    if not (room_element and isinstance(room_element, SpatialElement)):
        diagnostic_log.append(("(unresolved element)", "-", "-", "-", "-", "-", "SKIPPED - could not resolve to a Room element"))
        continue

    room_number = get_room_number(room_element)

    room_name_only = ""
    try:
        name_param = room_element.get_Parameter(BuiltInParameter.ROOM_NAME)
        if name_param and name_param.HasValue:
            room_name_only = name_param.AsString() or ""
    except Exception:
        pass

    room_name = "Unnamed"
    try:
        room_name = safe_str("{} - {}".format(
            room_number if room_number else "?",
            room_name_only if room_name_only else "?"
        ))
    except Exception:
        pass

    area_sqft = get_room_area_sqft(room_element)
    area_sqm_str = "{:.2f}".format(sqft_to_sqm(area_sqft))

    foh_boh_val = get_room_param_string(room_element, FOH_BOH_PARAM_NAME)
    gifa_key_val = get_room_param_string(room_element, GIFA_KEY_PARAM_NAME)
    code = get_room_finish_code(room_element)

    # 0. Unbound / unplaced room (no Area) - skip before anything else
    if area_sqft <= 0.0:
        diagnostic_log.append((
            room_name, area_sqm_str, foh_boh_val or "-", gifa_key_val or "-", code or "-", "-",
            "SKIPPED - unbound Room (no Area)"
        ))
        continue

    # 1. FOH BOH / GIFA Key exclusion filters
    skip_reason = None
    if foh_boh_val and "FOH" in foh_boh_val.upper():
        skip_reason = "'{}' contains 'FOH'".format(FOH_BOH_PARAM_NAME)
    elif gifa_key_val and "NON GIFA" in gifa_key_val.upper():
        skip_reason = "'{}' contains 'NON GIFA'".format(GIFA_KEY_PARAM_NAME)
    elif gifa_key_val and "TOILET" in gifa_key_val.upper():
        skip_reason = "'{}' contains 'TOILET'".format(GIFA_KEY_PARAM_NAME)

    if skip_reason:
        diagnostic_log.append((
            room_name, area_sqm_str, foh_boh_val or "-", gifa_key_val or "-", code or "-", "-",
            "SKIPPED - " + skip_reason
        ))
        continue

    # 2. Read Floor Finish code
    if not code:
        diagnostic_log.append((
            room_name, area_sqm_str, foh_boh_val or "-", gifa_key_val or "-", "-", "-",
            "SKIPPED - blank '{}' parameter".format(FLOOR_FINISH_PARAM_NAME)
        ))
        continue

    # 3. Resolve Floor Type (cache per code for this run)
    if code in code_to_type_cache:
        resolved_type = code_to_type_cache[code]
    else:
        matches = find_matching_floor_types(code, floor_types_dict)
        if not matches:
            resolved_type = None
        elif len(matches) == 1:
            resolved_type = matches[0][1]
        else:
            picked_name = forms.SelectFromList.show(
                [name for name, _ in matches],
                title="Multiple Floor Types match '{}' - pick one".format(code),
                multiselect=False
            )
            resolved_type = dict(matches).get(picked_name) if picked_name else None
        code_to_type_cache[code] = resolved_type

    if not resolved_type:
        diagnostic_log.append((
            room_name, area_sqm_str, foh_boh_val or "-", gifa_key_val or "-", code, "-",
            "SKIPPED - no Floor Type name starts with '{}'".format(code)
        ))
        continue

    # 4. Level = Room's own Level
    if transform:
        # Linked room: match host level by name
        l_level = None
        try:
            link_doc_local = room_element.Document
            l_level = link_doc_local.GetElement(room_element.LevelId)
        except Exception:
            pass
        if l_level:
            host_levels = FilteredElementCollector(doc).OfClass(Level).ToElements()
            for lvl in host_levels:
                if lvl.Name == l_level.Name:
                    target_level_id = lvl.Id
                    break
        if not target_level_id:
            if doc.ActiveView.GenLevel:
                target_level_id = doc.ActiveView.GenLevel.Id
            else:
                diagnostic_log.append((
                    room_name, area_sqm_str, foh_boh_val or "-", gifa_key_val or "-", code, "-",
                    "SKIPPED - no matching host Level for linked room"
                ))
                continue
    else:
        target_level_id = room_element.LevelId

    # 5. Boundary + Floor creation (structural enforced post-creation, see ensure_structural)
    loop_count = count_boundary_loops(room_element)
    try:
        new_floor, boundary_method = create_floor_from_room(
            doc, room_element, transform, resolved_type.Id, target_level_id, area_sqft
        )

        if new_floor:
            if height_offset_feet != 0.0:
                p = new_floor.get_Parameter(BuiltInParameter.FLOOR_HEIGHTABOVELEVEL_PARAM)
                if p:
                    p.Set(height_offset_feet)

            structural_status = ensure_structural(new_floor)
            saa_status = set_saa_floor_room_param(new_floor, room_name_only)

            floors_created += 1

            # Sanity check: does the created floor's actual area (Revit's own computed
            # value) match the Room's own Area parameter? A big mismatch usually means
            # Revit's room boundary calculation missed a bounding wall (commonly a
            # linked-model wall with "Room Bounding" disabled), so the floor bled
            # into a neighbor.
            area_note = ""
            floor_area_param = new_floor.get_Parameter(BuiltInParameter.HOST_AREA_COMPUTED)
            if area_sqft > 0 and floor_area_param and floor_area_param.HasValue:
                floor_area_sqft = floor_area_param.AsDouble()
                diff_pct = abs(floor_area_sqft - area_sqft) / area_sqft * 100.0
                if diff_pct > 5.0:
                    area_note = " | !! AREA MISMATCH: floor {:.2f} m2 vs room {:.2f} m2 ({:.0f}% off) - check Room Bounding on the dividing wall (often a linked model's Room Bounding setting)".format(
                        sqft_to_sqm(floor_area_sqft), sqft_to_sqm(area_sqft), diff_pct
                    )

            diagnostic_log.append((
                room_name, area_sqm_str, foh_boh_val or "-", gifa_key_val or "-", code,
                loop_count,
                "CREATED ({}) - Type: {} | Structural: {} | SAA_Floor-Room: {}{}".format(
                    boundary_method, get_type_name_safe(resolved_type), structural_status, saa_status, area_note
                )
            ))
        else:
            diagnostic_log.append((
                room_name, area_sqm_str, foh_boh_val or "-", gifa_key_val or "-", code,
                loop_count, "SKIPPED - " + boundary_method
            ))
    except Exception:
        diagnostic_log.append((
            room_name, area_sqm_str, foh_boh_val or "-", gifa_key_val or "-", code,
            loop_count, "SKIPPED - error: " + safe_str(traceback.format_exc())
        ))

t.Commit()

# ---------------- REPORT ----------------

output.print_md("**Rooms processed:** {}   |   **Floors created:** {}".format(len(sel_refs), floors_created))
output.print_md("_'Boundary Loops' > 1 means the Room has an island (interior column/wall poking "
                 "into it) - this can distort or split the resulting floor shape._")
output.print_table(
    table_data=diagnostic_log,
    columns=["Room", "Area (m2)", "FOH BOH", "GIFA Key", "Floor Finish", "Boundary Loops", "Result"]
)

if floors_created == 0:
    forms.alert("No floors created. See the output window table for the reason per room.", warn_icon=True)