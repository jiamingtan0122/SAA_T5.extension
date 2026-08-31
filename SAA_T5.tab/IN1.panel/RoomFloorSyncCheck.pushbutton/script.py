# -*- coding: utf-8 -*-
__title__ = "Room Floor\nSync Check"
__author__ = "JM"
__doc__ = """Version = 1.0
Date    = 26.08.2026
Description:
Scans BOH Rooms and flags any associated Floor that has NOT been updated
to match the Room's current boundary - colouring it red in the active
view so out-of-date slabs are obvious at a glance.

- Step 1: Open the view you want checked, then click the button. No
  selection needed. SCOPE = CURRENT VIEW ONLY: Rooms come from the host
  model (view-scoped, so VG/view filters/crop/phase apply as normal) AND
  from any linked model instances visible in that view, narrowed to the
  same Level name as the view (a link has no concept of "this host view",
  so level-name matching is the practical stand-in on a plan view - other
  view types include every linked Room). Floors are collected the SAME
  way (host + every visible link), view-scoped for the host portion.
- Logic:
    - A Room is INCLUDED only if:
        - 'FOH BOH' parameter == "BOH"
        - 'GIFA Key' parameter is NOT "TOILET" and NOT "NON GIFA"
        - it is bound/placed (Area > 0)
    - A Floor is treated as a Room's floor if its solid geometry actually
      overlaps/intersects that Room's footprint (bounding-box pre-filter,
      then a real Boolean intersection tried at Finish -> CoreCenter ->
      Center boundary locations in turn, stopping at the first that hits
      - Center is the universal fallback since Finish/CoreCenter can
      return an EMPTY boundary outright, not just a shifted one, when a
      bounding wall has no core/finish layers e.g. a curtain wall).
    - A Floor is flagged OUT OF SYNC if EITHER of the following is true:
        1. SHAPE/AREA MISMATCH beyond AREA_MISMATCH_PCT (default 3%).
           Measured as (excess area + missing area) / room area, using a
           thin solid prism built from the Room's boundary Boolean'd
           against the Floor's actual solid. This is checked against BOTH
           the Room's 'Finish' boundary AND its 'CoreCenter' boundary -
           some slabs on this project are modelled to the wall centreline
           rather than the finish face, and comparing against Finish only
           would falsely flag every one of those. Whichever boundary
           location gives the smaller mismatch is treated as the Floor's
           intended reference, so a correctly-built centreline slab is
           not flagged just because it doesn't match the finish face.
        2. WALL CLASH - the Floor solid intrudes past a room-bounding
           Wall's core centreline (+CLASH_DEPTH_TOL) on the far side, i.e.
           the slab edge was not pulled back after that wall moved. Built
           per boundary segment: a thin box from the wall's centreline
           outward by (wall width / 2 + tolerance), Boolean-intersected
           with the Floor solid.
    - Flagged Floors get a red solid-fill + red line override in the
      ACTIVE VIEW only (Element overrides, not the Floor Type itself -
      nothing is permanently changed on the elements).
    - Every Room/Floor pair checked (flagged or not) is printed in ONE
      table in the output window, with its mismatch % and clash result,
      so "0 flagged" always comes with the full picture, not just silence.

- Known limits (extend as needed):
    - Linked-Room scoping to "this view" is by Level NAME match on plan
      views only - a 3D/section view includes every linked Room, and a
      level naming mismatch between host and link will under- or
      over-include rooms silently.
    - Linked Floor overrides use the LinkElementId API (Revit 2022+) -
      on older Revit versions a flagged linked Floor may not colour, but
      it still appears in the results table either way.
    - The wall-clash check treats each boundary segment as a straight
      chord, which is adequate for flagging but approximate on arcs.
    - Tolerances (AREA_MISMATCH_PCT, CLASH_DEPTH_TOL_FT) are constants
      near the top of the file - tune them against real results first.
"""

from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, FilteredElementCollector,
    Floor, Wall, RevitLinkInstance, BoundingBoxXYZ, LinkElementId, ElementId,
    SpatialElementBoundaryOptions, SpatialElementBoundaryLocation,
    CurveLoop, Line, XYZ, Transform, Solid, GeometryInstance, Options,
    SolidUtils, GeometryCreationUtilities, BooleanOperationsUtils,
    BooleanOperationsType, OverrideGraphicSettings, Color, FillPatternElement,
    Transaction, IFailuresPreprocessor, FailureProcessingResult, FailureSeverity
)
from Autodesk.Revit.DB.Architecture import Room
from pyrevit import revit, forms, script
import traceback

# ---------------- INITIALIZATION ----------------
doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

FOH_BOH_PARAM_NAME = "FOH BOH"
GIFA_KEY_PARAM_NAME = "GIFA Key"

FT2MM = 304.8
AREA_MISMATCH_PCT = 0.03                              # 3% of room area -> flag
CLASH_DEPTH_TOL_FT = 20.0 / FT2MM                      # 20mm past wall centreline
MIN_CLASH_VOL_FT3 = ((200.0 / FT2MM) ** 2) * (50.0 / FT2MM)  # ~200x200x50mm sliver ignored
PRISM_PAD_FT = 50.0 / FT2MM                            # vertical pad above/below floor


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
    """Auto-accepts resolvable warnings so the check doesn't stall on dialogs."""
    def PreprocessFailures(self, failures_accessor):
        failures = failures_accessor.GetFailureMessages()
        for f in failures:
            if f.GetSeverity() == FailureSeverity.Warning:
                failures_accessor.DeleteWarning(f)
        return FailureProcessingResult.Continue


# ---------------- HELPERS ----------------

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


def room_qualifies(room):
    """
    Returns (True, None) if the room passes the BOH filter, otherwise
    (False, reason_string) so the caller can log WHY it was skipped.
    """
    if get_room_area_sqft(room) <= 0.0:
        return False, "unbound Room (no Area)"

    foh_boh_val = get_room_param_string(room, FOH_BOH_PARAM_NAME)
    if not foh_boh_val or foh_boh_val.upper() != "BOH":
        return False, "'{}' is not 'BOH'".format(FOH_BOH_PARAM_NAME)

    gifa_val = get_room_param_string(room, GIFA_KEY_PARAM_NAME)
    if gifa_val:
        gifa_upper = gifa_val.upper()
        if gifa_upper == "TOILET":
            return False, "'{}' is 'TOILET'".format(GIFA_KEY_PARAM_NAME)
        if gifa_upper == "NON GIFA":
            return False, "'{}' is 'NON GIFA'".format(GIFA_KEY_PARAM_NAME)

    return True, None


def bb_overlap(bb1, bb2):
    """Quick bounding-box overlap pre-filter before any real geometry work."""
    if not bb1 or not bb2:
        return False
    return not (bb1.Max.X < bb2.Min.X or bb1.Min.X > bb2.Max.X or
                bb1.Max.Y < bb2.Min.Y or bb1.Min.Y > bb2.Max.Y or
                bb1.Max.Z < bb2.Min.Z or bb1.Min.Z > bb2.Max.Z)


def transform_bbox_to_host(bbox, transform):
    """A Room's own get_BoundingBox() is in ITS document's local coordinate
    system - for a linked Room that's the link's local space, not the host.
    Transforms all 8 corners and rebuilds an axis-aligned box in host space
    (needed since the link may be rotated relative to the host)."""
    if bbox is None:
        return None
    if transform is None:
        return bbox
    xs, ys, zs = [], [], []
    for dx in (bbox.Min.X, bbox.Max.X):
        for dy in (bbox.Min.Y, bbox.Max.Y):
            for dz in (bbox.Min.Z, bbox.Max.Z):
                p = transform.OfPoint(XYZ(dx, dy, dz))
                xs.append(p.X)
                ys.append(p.Y)
                zs.append(p.Z)
    new_bb = BoundingBoxXYZ()
    new_bb.Min = XYZ(min(xs), min(ys), min(zs))
    new_bb.Max = XYZ(max(xs), max(ys), max(zs))
    return new_bb


def reverse_loop(loop):
    """Rebuilds a CurveLoop in the opposite winding direction (reverses
    curve order AND each individual curve's own direction)."""
    curves = list(loop)
    new_loop = CurveLoop()
    for c in reversed(curves):
        new_loop.Append(c.CreateReversed())
    return new_loop


def get_boundary_loops(room, boundary_location, transform=None):
    """Builds CurveLoops directly from Revit's own room boundary segments
    at the given SpatialElementBoundaryLocation (Finish or CoreCenter).
    If the Room is in a link, pass its transform to bring the curves into
    host coordinates (so they line up with the host Floor's geometry).

    Revit does not guarantee these loops wind counter-clockwise when
    viewed from +Z, which is what GeometryCreationUtilities.
    CreateExtrusionGeometry (in make_prism) expects - get it wrong and
    the extrusion doesn't throw, it just comes out with ~zero enclosed
    volume, which then fails every downstream check silently (looks
    exactly like "no floor found" even on an obviously overlapping
    Floor). So: check the FIRST loop's winding and, if it's not CCW,
    reverse every loop together (this preserves the outer-boundary-vs-
    island relative winding Revit already set up internally - it's only
    the absolute direction that needs correcting).
    """
    options = SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = boundary_location
    try:
        segments_list = room.GetBoundarySegments(options)
    except Exception:
        return []
    if not segments_list:
        return []

    loops = []
    for room_outline in segments_list:
        curves = []
        for seg in room_outline:
            c = seg.GetCurve()
            if not c:
                continue
            if transform is not None:
                c = c.CreateTransformed(transform)
            curves.append(c)
        if len(curves) < 3:
            continue
        try:
            loop = CurveLoop()
            for c in curves:
                loop.Append(c)
            loops.append(loop)
        except Exception:
            continue

    if not loops:
        return []

    try:
        needs_flip = not loops[0].IsCounterclockwise(XYZ.BasisZ)
    except Exception:
        needs_flip = False

    if needs_flip:
        loops = [reverse_loop(l) for l in loops]

    return loops


def make_prism(loops, base_z, height):
    """Extrudes a set of CurveLoops into a thin solid starting at base_z."""
    if not loops or height <= 1e-6:
        return None
    try:
        solid = GeometryCreationUtilities.CreateExtrusionGeometry(loops, XYZ.BasisZ, height)
        tf = Transform.CreateTranslation(XYZ(0, 0, base_z))
        return SolidUtils.CreateTransformed(solid, tf)
    except Exception:
        return None


def get_largest_solid(geom_elem):
    """Walks a GeometryElement (incl. GeometryInstances) and returns the
    largest-volume Solid found - the Floor's actual slab geometry."""
    best = None
    for g in geom_elem:
        if isinstance(g, Solid) and g.Volume > 1e-6:
            if best is None or g.Volume > best.Volume:
                best = g
        elif isinstance(g, GeometryInstance):
            for gg in g.GetInstanceGeometry():
                if isinstance(gg, Solid) and gg.Volume > 1e-6:
                    if best is None or gg.Volume > best.Volume:
                        best = gg
    return best


def bool_op(solid_a, solid_b, op):
    """Boolean op wrapped safe - non-overlapping solids raise in the Revit
    API rather than returning an empty result, so treat that as 'nothing'."""
    if solid_a is None or solid_b is None:
        return None
    try:
        return BooleanOperationsUtils.ExecuteBooleanOperation(solid_a, solid_b, op)
    except Exception:
        return None


def solid_volume(solid):
    try:
        return solid.Volume if solid else 0.0
    except Exception:
        return 0.0


def compute_room_floor_check(room, transform, floor_solid, prism_base_z, prism_height):
    """
    Tries the Room boundary at Finish -> CoreCenter -> Center, in that
    order of preference/accuracy, and returns (matched, ratio):

        matched = True as soon as ANY of the three actually intersects
        the Floor solid with non-negligible volume - i.e. this Floor is
        genuinely associated with this Room, however badly out of sync.
        False only means no boundary location produced geometry that
        touches the Floor at all (wrong Floor, or the Room has no real
        Floor yet).

        ratio = the SMALLEST (excess+missing)/room_area among the
        locations that matched - see module docstring for why Finish vs
        CoreCenter both get tried. Center is the last-resort fallback
        because it's the only boundary location Revit can always compute
        (Finish/CoreCenter can return an EMPTY loop outright - not just a
        different position - when even one bounding wall has no
        core/finish layers, e.g. a curtain wall or a wall with no
        compound structure; without Center as a fallback, every Room
        touching a wall like that would wrongly report "no floor found"
        even when a Floor clearly covers most of it).

    `transform` is the Room's link transform, or None for a host Room.
    """
    best_ratio = None
    matched = False
    for loc in (SpatialElementBoundaryLocation.Finish,
                SpatialElementBoundaryLocation.CoreCenter,
                SpatialElementBoundaryLocation.Center):
        loops = get_boundary_loops(room, loc, transform)
        prism = make_prism(loops, prism_base_z, prism_height)
        if prism is None:
            continue
        room_vol = solid_volume(prism)
        if room_vol <= 1e-6:
            continue
        overlap = bool_op(floor_solid, prism, BooleanOperationsType.Intersect)
        if solid_volume(overlap) <= 1e-6:
            continue  # this boundary location doesn't actually touch the floor

        matched = True
        excess = bool_op(floor_solid, prism, BooleanOperationsType.Difference)
        missing = bool_op(prism, floor_solid, BooleanOperationsType.Difference)
        ratio = (solid_volume(excess) + solid_volume(missing)) / room_vol
        if best_ratio is None or ratio < best_ratio:
            best_ratio = ratio
    return matched, (best_ratio if best_ratio is not None else 1.0)


def wall_clash(room, room_doc, transform, floor_solid, prism_base_z, prism_height):
    """
    True if the Floor intrudes past a bounding Wall's core centreline
    (+CLASH_DEPTH_TOL) on the far side - i.e. the slab wasn't pulled back
    after that wall moved. Uses CoreCenter boundary segments so the
    reference line sits exactly on each wall's centreline, matching how
    the "middle of wall" slabs on this project were built.

    All geometry (normal direction, IsPointInRoom test) is worked out in
    the Room's OWN document coordinates first, since IsPointInRoom and the
    bounding Wall's ElementId only resolve correctly there (room_doc is
    the link's document for a linked Room). Only the final box handed to
    the Boolean op is transformed into host coordinates, to match the
    host Floor's geometry.
    """
    options = SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.CoreCenter
    try:
        segments_list = room.GetBoundarySegments(options)
    except Exception:
        return False
    if not segments_list:
        return False

    for room_outline in segments_list:
        for seg in room_outline:
            wall = room_doc.GetElement(seg.ElementId)
            if not isinstance(wall, Wall):
                continue
            try:
                width = wall.Width
            except Exception:
                continue

            local_curve = seg.GetCurve()
            if local_curve is None:
                continue

            local_p0 = local_curve.GetEndPoint(0)
            local_p1 = local_curve.GetEndPoint(1)
            local_mid = local_curve.Evaluate(0.5, True)
            deriv = local_curve.ComputeDerivatives(0.5, True)
            tangent = deriv.BasisX.Normalize()
            normal = XYZ(-tangent.Y, tangent.X, 0).Normalize()

            test_in = local_mid - normal * 0.1
            try:
                inside_room = room.IsPointInRoom(XYZ(test_in.X, test_in.Y, local_mid.Z))
            except Exception:
                inside_room = True
            outward = normal if inside_room else -normal

            reach = width / 2.0 + CLASH_DEPTH_TOL_FT
            offset_vec = outward * reach

            local_pts = [local_p0, local_p1, local_p1 + offset_vec, local_p0 + offset_vec]
            if transform is not None:
                host_pts = [transform.OfPoint(p) for p in local_pts]
            else:
                host_pts = local_pts

            try:
                far_loop = CurveLoop()
                for i in range(4):
                    far_loop.Append(Line.CreateBound(host_pts[i], host_pts[(i + 1) % 4]))
                if not far_loop.IsCounterclockwise(XYZ.BasisZ):
                    far_loop = reverse_loop(far_loop)
            except Exception:
                continue

            far_solid = make_prism([far_loop], prism_base_z, prism_height)
            intersection = bool_op(floor_solid, far_solid, BooleanOperationsType.Intersect)
            if solid_volume(intersection) > MIN_CLASH_VOL_FT3:
                return True
    return False


def get_solid_fill_pattern_id():
    for fp in FilteredElementCollector(doc).OfClass(FillPatternElement):
        try:
            if fp.GetFillPattern().IsSolidFill:
                return fp.Id
        except Exception:
            continue
    return None


# ---------------- MAIN LOGIC ----------------

output.print_md("## Room Floor Sync Check - Run Log")

view = doc.ActiveView

try:
    link_instances = list(FilteredElementCollector(doc, view.Id).OfClass(RevitLinkInstance)
                           .WhereElementIsNotElementType())
except Exception:
    link_instances = list(FilteredElementCollector(doc).OfClass(RevitLinkInstance)
                           .WhereElementIsNotElementType())

# Rooms come from BOTH the host model AND any linked model instances that
# are actually visible in this view. Each entry is (room, room_doc,
# transform, source_label) - transform is None for host Rooms, or the
# link's placement transform for linked Rooms (needed to bring their
# geometry into host coordinates for every Boolean check below).
room_entries = []

try:
    host_rooms = list(FilteredElementCollector(doc, view.Id).OfCategory(BuiltInCategory.OST_Rooms)
                       .WhereElementIsNotElementType())
except Exception:
    host_rooms = list(FilteredElementCollector(doc).OfCategory(BuiltInCategory.OST_Rooms)
                       .WhereElementIsNotElementType())
for r in host_rooms:
    room_entries.append((r, doc, None, "Host"))

view_level_name = None
if view.GenLevel:
    view_level_name = view.GenLevel.Name

for link_inst in link_instances:
    link_doc = link_inst.GetLinkDocument()
    if not link_doc:
        continue  # unloaded link
    link_transform = link_inst.GetTotalTransform()
    link_rooms = list(FilteredElementCollector(link_doc).OfCategory(BuiltInCategory.OST_Rooms)
                       .WhereElementIsNotElementType())
    link_label = safe_str(link_inst.Name)
    for r in link_rooms:
        # Narrow linked rooms to the current view's level where possible -
        # a link has no notion of the host view, so level-name matching is
        # the practical stand-in for "current view only" on a plan view.
        if view_level_name:
            try:
                r_level = link_doc.GetElement(r.LevelId)
                if r_level and r_level.Name != view_level_name:
                    continue
            except Exception:
                pass
        room_entries.append((r, link_doc, link_transform, link_label))

# Floors: same dual-source treatment as Rooms above, so a project that
# keeps slabs in a structural link (or split between host and link) still
# gets matched correctly. Each entry is (floor, transform, link_inst_id,
# source_label) - link_inst_id is None for a host Floor, needed later to
# override a LINKED Floor's graphics via LinkElementId.
floor_entries = []

try:
    host_floors = list(FilteredElementCollector(doc, view.Id).OfClass(Floor).WhereElementIsNotElementType())
except Exception:
    host_floors = list(FilteredElementCollector(doc).OfClass(Floor).WhereElementIsNotElementType())
for f in host_floors:
    floor_entries.append((f, None, None, "Host"))

for link_inst in link_instances:
    link_doc = link_inst.GetLinkDocument()
    if not link_doc:
        continue
    link_transform = link_inst.GetTotalTransform()
    link_floors = list(FilteredElementCollector(link_doc).OfClass(Floor).WhereElementIsNotElementType())
    link_label = safe_str(link_inst.Name)
    for f in link_floors:
        floor_entries.append((f, link_transform, link_inst.Id, link_label))

output.print_md("_Scope: current view only - **{}** ({} link(s) checked)_".format(
    safe_str(view.Name), len(link_instances)))
output.print_md("**Rooms collected:** {} (host: {}, from links: {})   |   **Floors collected:** {} (host: {}, from links: {})".format(
    len(room_entries), len(host_rooms), len(room_entries) - len(host_rooms),
    len(floor_entries), len(host_floors), len(floor_entries) - len(host_floors)
))

if not floor_entries:
    forms.alert(
        "0 Floors were collected for this view/links, so every Room will show 'NO FLOOR "
        "FOUND' regardless of geometry. Likely causes: the Floors category is switched off "
        "in Visibility/Graphics for this view, the floors sit on a phase this view doesn't "
        "show, they're in a Design Option not active here, or a relevant workset isn't open.",
        warn_icon=True
    )

diagnostic_log = []   # one row per checked Room/Floor pair
skipped_rooms = []    # rooms excluded by the BOH filter
flagged_host_ids = set()
flagged_link_keys = set()   # (link_instance_id_int, floor_id_int)

t = Transaction(doc, "Check Room-Floor Sync (BOH)")
fail_opts = t.GetFailureHandlingOptions()
fail_opts.SetFailuresPreprocessor(AutoOkFailurePreprocessor())
t.SetFailureHandlingOptions(fail_opts)
t.Start()

try:
    for room, room_doc, transform, source_label in room_entries:
        room_number = room.get_Parameter(BuiltInParameter.ROOM_NUMBER)
        room_number_val = room_number.AsString() if room_number and room_number.HasValue else "?"
        room_name_param = room.get_Parameter(BuiltInParameter.ROOM_NAME)
        room_name_val = room_name_param.AsString() if room_name_param and room_name_param.HasValue else "?"
        room_label = safe_str("[{}] {} - {}".format(source_label, room_number_val, room_name_val))

        qualifies, skip_reason = room_qualifies(room)
        if not qualifies:
            skipped_rooms.append((room_label, skip_reason))
            continue

        room_bb_local = room.get_BoundingBox(None)
        if not room_bb_local:
            skipped_rooms.append((room_label, "no bounding box"))
            continue
        room_bb = transform_bbox_to_host(room_bb_local, transform)

        matched_any_floor = False
        for floor, floor_transform, floor_link_id, floor_source in floor_entries:
            floor_bb_local = floor.get_BoundingBox(None)
            if not floor_bb_local:
                continue
            floor_bb = transform_bbox_to_host(floor_bb_local, floor_transform)
            if not bb_overlap(room_bb, floor_bb):
                continue

            floor_geom = floor.get_Geometry(Options())
            floor_solid = get_largest_solid(floor_geom)
            if floor_solid is None:
                continue
            if floor_transform is not None:
                try:
                    floor_solid = SolidUtils.CreateTransformed(floor_solid, floor_transform)
                except Exception:
                    continue

            base_z = floor_bb.Min.Z - PRISM_PAD_FT
            height = (floor_bb.Max.Z - floor_bb.Min.Z) + 2 * PRISM_PAD_FT

            matched, ratio = compute_room_floor_check(room, transform, floor_solid, base_z, height)
            if not matched:
                continue

            matched_any_floor = True
            clash = wall_clash(room, room_doc, transform, floor_solid, base_z, height)
            out_of_sync = (ratio > AREA_MISMATCH_PCT) or clash

            diagnostic_log.append((
                room_label,
                "[{}] {}".format(floor_source, floor.Id.IntegerValue),
                "{:.1f}%".format(ratio * 100.0),
                "YES" if clash else "no",
                "OUT OF SYNC" if out_of_sync else "ok"
            ))
            if out_of_sync:
                if floor_link_id is None:
                    flagged_host_ids.add(floor.Id)
                else:
                    flagged_link_keys.add((floor_link_id.IntegerValue, floor.Id.IntegerValue))

        if not matched_any_floor:
            diagnostic_log.append((room_label, "-", "-", "-", "NO FLOOR FOUND"))

    red = Color(255, 0, 0)
    ogs = OverrideGraphicSettings()
    ogs.SetProjectionLineColor(red)
    solid_fill_id = get_solid_fill_pattern_id()
    if solid_fill_id:
        ogs.SetSurfaceForegroundPatternColor(red)
        ogs.SetSurfaceForegroundPatternId(solid_fill_id)

    for fid in flagged_host_ids:
        view.SetElementOverrides(fid, ogs)

    link_override_failures = 0
    for (link_id_int, floor_id_int) in flagged_link_keys:
        try:
            lei = LinkElementId(ElementId(link_id_int), ElementId(floor_id_int))
            view.SetElementOverrides(lei, ogs)
        except Exception:
            link_override_failures += 1

    t.Commit()
except Exception:
    t.RollBack()
    output.print_md("**Error:**")
    output.print_code(safe_str(traceback.format_exc()))
    script.exit()

# ---------------- REPORT ----------------

total_flagged = len(flagged_host_ids) + len(flagged_link_keys)

output.print_md("**BOH rooms checked:** {}   |   **Rooms skipped (filter):** {}   |   **Floors flagged:** {}".format(
    len(room_entries) - len(skipped_rooms), len(skipped_rooms), total_flagged
))
if diagnostic_log:
    output.print_table(
        table_data=diagnostic_log,
        columns=["Room", "Floor", "Mismatch %", "Wall Clash", "Result"]
    )
else:
    output.print_md("_No qualifying BOH Room/Floor pairs found in this view's scope._")

if skipped_rooms:
    output.print_md("_Rooms excluded by the 'FOH BOH' / 'GIFA Key' filter:_")
    output.print_table(
        table_data=skipped_rooms,
        columns=["Room", "Reason"]
    )

if link_override_failures:
    output.print_md(
        "_{} linked Floor(s) were flagged but could not be graphically overridden "
        "(LinkElementId override may not be supported on this Revit/pyRevit version) - "
        "see the table above for which ones._".format(link_override_failures)
    )

if total_flagged:
    forms.alert(
        "{} floor(s) flagged out-of-sync and coloured red in the active view.".format(total_flagged),
        warn_icon=True
    )
elif floor_entries:
    forms.alert("No out-of-sync floors found - all checked BOH floors match their room boundary.")