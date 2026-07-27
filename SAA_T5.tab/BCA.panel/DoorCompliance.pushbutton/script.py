# -*- coding: utf-8 -*-
__title__ = "PWD Door\nCheck"
__author__ = "JM"
__doc__ = "Version = 1.5 | Date = 15.04.2025 | Host doors: uses PWD Clearance subcategory solid, sets Show PWD Check parameter. Linked doors: computes clearance zone from orientation (1500mm pull, 1200mm push), reports PASS/FAIL only."

from pyrevit import revit, script
from pyrevit.forms import alert
from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter,
    BoundingBoxIntersectsFilter,
    FilteredElementCollector,
    Outline,
    RevitLinkInstance,
    Transaction, XYZ, Transform,
    GeometryInstance, Options,
    BoundingBoxXYZ,
    GraphicsStyleType,
    LocationPoint,
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
import sys
import math

output = script.get_output()
doc    = revit.doc
uidoc  = revit.uidoc
view   = doc.ActiveView


# =========================================================
# UNIT CONVERSION
# =========================================================
def mm(v):
    return v / 304.8


# =========================================================
# CLEARANCE DIMENSIONS (linked doors)
# =========================================================
PULL_DEPTH = mm(1500)
PULL_WIDTH = mm(1500)
PUSH_DEPTH = mm(1200)
PUSH_WIDTH = mm(1200)
BOX_HEIGHT = mm(2100)


# =========================================================
# OBSTRUCTION CATEGORIES
# =========================================================
OBSTRUCTION_CATS = [
    BuiltInCategory.OST_Walls,
    BuiltInCategory.OST_StructuralColumns,
    BuiltInCategory.OST_Columns,
    BuiltInCategory.OST_PlumbingFixtures,
    BuiltInCategory.OST_Furniture,
    BuiltInCategory.OST_Casework,
    BuiltInCategory.OST_GenericModel,
    BuiltInCategory.OST_MechanicalEquipment,
]

PARAM_NAME  = "Show PWD Check"
SUBCAT_NAME = "PWD Clearance"


# =========================================================
# LINK MAP  { inst_id: (inst, lnk_doc, xform) }
# =========================================================
def build_link_map():
    result = {}
    for inst in (FilteredElementCollector(doc)
                 .OfClass(RevitLinkInstance).ToElements()):
        try:
            lnk_doc = inst.GetLinkDocument()
            if lnk_doc:
                result[inst.Id.IntegerValue] = (
                    inst, lnk_doc, inst.GetTotalTransform()
                )
        except Exception:
            pass
    return result

LINK_MAP = build_link_map()


# =========================================================
# SUBCATEGORY GRAPHICSSTYLE IDs
# =========================================================
def get_subcat_style_ids(subcat_name):
    # Return a set of GraphicsStyle element IDs matching the subcategory name.
    style_ids = set()
    door_cat  = doc.Settings.Categories.get_Item(BuiltInCategory.OST_Doors)
    if door_cat is None:
        return style_ids
    for sub in door_cat.SubCategories:
        if sub.Name == subcat_name:
            gs = sub.GetGraphicsStyle(GraphicsStyleType.Projection)
            if gs:
                style_ids.add(gs.Id.IntegerValue)
            gs2 = sub.GetGraphicsStyle(GraphicsStyleType.Cut)
            if gs2:
                style_ids.add(gs2.Id.IntegerValue)
    return style_ids


# =========================================================
# EXTRACT CLEARANCE BBOX FROM FAMILY GEOMETRY (host doors)
# =========================================================
def get_clearance_bbox_from_geometry(door_el, subcat_ids, geo_options, link_xform=None):
    # Walk door geometry. Collect solid vertices whose GraphicsStyle
    # matches the PWD Clearance subcategory. Transform to host model
    # coordinates. Returns BoundingBoxXYZ or None if not found.
    pts = []

    geo_elem = door_el.get_Geometry(geo_options)
    if geo_elem is None:
        return None

    for obj in geo_elem:
        if isinstance(obj, GeometryInstance):
            xform   = obj.Transform
            sym_geo = obj.GetSymbolGeometry()
            if sym_geo is None:
                continue
            for solid in sym_geo:
                try:
                    gs_id = solid.GraphicsStyleId
                except Exception:
                    gs_id = None
                if gs_id is not None and gs_id.IntegerValue in subcat_ids:
                    for face in solid.Faces:
                        mesh = face.Triangulate()
                        if mesh:
                            for v in mesh.Vertices:
                                world_pt = xform.OfPoint(v)
                                if link_xform is not None:
                                    world_pt = link_xform.OfPoint(world_pt)
                                pts.append(world_pt)

    if not pts:
        return None

    xs = [p.X for p in pts]
    ys = [p.Y for p in pts]
    zs = [p.Z for p in pts]

    bb     = BoundingBoxXYZ()
    bb.Min = XYZ(min(xs), min(ys), min(zs))
    bb.Max = XYZ(max(xs), max(ys), max(zs))
    return bb


# =========================================================
# COMPUTE CLEARANCE BBOXES FROM ORIENTATION (linked doors)
# Returns (pull_bbox, push_bbox) or (None, None) on failure.
# =========================================================
def norm2d(v):
    mag = math.sqrt(v.X * v.X + v.Y * v.Y)
    if mag < 1e-9:
        return XYZ(1, 0, 0)
    return XYZ(v.X / mag, v.Y / mag, 0.0)


def make_oriented_bbox(origin_pt, facing, hand, depth, width, z_base):
    # Build 4 corners of the clearance rectangle then wrap in BoundingBoxXYZ.
    # origin_pt is the wall face centre point.
    half_w = width / 2.0
    p0 = XYZ(origin_pt.X - hand.X * half_w,
             origin_pt.Y - hand.Y * half_w, z_base)
    p1 = XYZ(origin_pt.X + hand.X * half_w,
             origin_pt.Y + hand.Y * half_w, z_base)
    p2 = XYZ(p1.X + facing.X * depth,
             p1.Y + facing.Y * depth, z_base + BOX_HEIGHT)
    p3 = XYZ(p0.X + facing.X * depth,
             p0.Y + facing.Y * depth, z_base + BOX_HEIGHT)

    pts = [p0, p1, p2, p3]
    xs  = [p.X for p in pts]
    ys  = [p.Y for p in pts]
    zs  = [z_base, z_base + BOX_HEIGHT]

    bb     = BoundingBoxXYZ()
    bb.Min = XYZ(min(xs), min(ys), min(zs))
    bb.Max = XYZ(max(xs), max(ys), max(zs))
    return bb


def get_clearance_bboxes_from_orientation(door_el, link_xform=None):
    # Returns (pull_bbox, push_bbox) computed from door orientation.
    # Used for linked doors that have no PWD Clearance family solid.
    loc = door_el.Location
    if not isinstance(loc, LocationPoint):
        return None, None

    pt = loc.Point

    try:
        facing_raw = door_el.FacingOrientation
        hand_raw   = door_el.HandOrientation
    except Exception:
        return None, None

    if link_xform is not None:
        facing = norm2d(link_xform.OfVector(facing_raw))
        hand   = norm2d(link_xform.OfVector(hand_raw))
        pt     = link_xform.OfPoint(pt)
    else:
        facing = norm2d(facing_raw)
        hand   = norm2d(hand_raw)

    # Door width
    door_width = mm(900)
    try:
        p = door_el.Symbol.get_Parameter(BuiltInParameter.DOOR_WIDTH)
        if p and p.AsDouble() > 0:
            door_width = p.AsDouble()
    except Exception:
        pass

    # Wall thickness for face offset
    wall_thick = mm(200)
    try:
        host = door_el.Host
        if host:
            wall_thick = host.Width
    except Exception:
        pass

    half_wall = wall_thick / 2.0
    z         = pt.Z

    # Wall face centres
    pull_face = XYZ(pt.X + facing.X * half_wall,
                    pt.Y + facing.Y * half_wall, z)
    push_face = XYZ(pt.X - facing.X * half_wall,
                    pt.Y - facing.Y * half_wall, z)

    pull_bb = make_oriented_bbox(pull_face,  facing,        hand, PULL_DEPTH, PULL_WIDTH, z)
    push_bb = make_oriented_bbox(push_face,  facing.Negate(), hand, PUSH_DEPTH, PUSH_WIDTH, z)

    return pull_bb, push_bb


# =========================================================
# OBSTRUCTION CHECK  (host + linked models)
# =========================================================
def has_obstruction(bb, exclude_door_id, exclude_wall_id):
    outline   = Outline(bb.Min, bb.Max)
    bb_filter = BoundingBoxIntersectsFilter(outline)

    collector = (FilteredElementCollector(doc, view.Id)
                 .WhereElementIsNotElementType()
                 .WherePasses(bb_filter))

    obstruction_ids = set(int(c) for c in OBSTRUCTION_CATS)

    for el in collector:
        if el.Id == exclude_door_id:
            continue
        if exclude_wall_id and el.Id == exclude_wall_id:
            continue
        cat = el.Category
        if cat is None:
            continue
        if cat.Id.IntegerValue in obstruction_ids:
            return True

    # Check linked models
    for _lid, (inst, lnk_doc, xform) in LINK_MAP.items():
        try:
            inv    = xform.Inverse
            lk_min = inv.OfPoint(bb.Min)
            lk_max = inv.OfPoint(bb.Max)
            lk_outline = Outline(
                XYZ(min(lk_min.X, lk_max.X),
                    min(lk_min.Y, lk_max.Y),
                    min(lk_min.Z, lk_max.Z)),
                XYZ(max(lk_min.X, lk_max.X),
                    max(lk_min.Y, lk_max.Y),
                    max(lk_min.Z, lk_max.Z))
            )
            lk_filter = BoundingBoxIntersectsFilter(lk_outline)
            for cat in OBSTRUCTION_CATS:
                try:
                    hits = (FilteredElementCollector(lnk_doc)
                            .OfCategory(cat)
                            .WhereElementIsNotElementType()
                            .WherePasses(lk_filter)
                            .ToElements())
                    if hits:
                        return True
                except Exception:
                    pass
        except Exception:
            pass

    return False


# =========================================================
# HOST DOOR PICKER
# =========================================================
class HostDoorFilter(ISelectionFilter):
    def __init__(self):
        self._cat = int(BuiltInCategory.OST_Doors)
    def AllowElement(self, elem):
        try:
            return (elem.Category and
                    elem.Category.Id.IntegerValue == self._cat)
        except Exception:
            return False
    def AllowReference(self, ref, point):
        return True


def pick_host_doors():
    resolved = []
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            HostDoorFilter(),
            "Pick HOST doors — Finish when done, ESC to skip"
        )
    except Exception:
        refs = []
    door_cat = int(BuiltInCategory.OST_Doors)
    seen = set()
    for ref in refs:
        try:
            elem = doc.GetElement(ref.ElementId)
            if elem is None:
                continue
            cat = elem.Category
            if cat is None or cat.Id.IntegerValue != door_cat:
                continue
            iid = ref.ElementId.IntegerValue
            if iid not in seen:
                seen.add(iid)
                resolved.append((elem, Transform.Identity, "Host"))
        except Exception:
            continue
    return resolved


# =========================================================
# LINKED DOOR PICKER
# =========================================================
class LinkedDoorFilter(ISelectionFilter):
    def __init__(self, link_map):
        self._door_cat = int(BuiltInCategory.OST_Doors)
        self._link_map = link_map

    def AllowElement(self, elem):
        # Allow RevitLinkInstance so user can click into linked models
        try:
            return isinstance(elem, RevitLinkInstance)
        except Exception:
            return False

    def AllowReference(self, ref, point):
        # Validate the referenced element inside the link is a door
        try:
            inst_id = ref.ElementId.IntegerValue
            if inst_id not in self._link_map:
                return False
            _, lnk_doc, _ = self._link_map[inst_id]
            linked_elem   = lnk_doc.GetElement(ref.LinkedElementId)
            if linked_elem is None:
                return False
            cat = linked_elem.Category
            return (cat is not None and
                    cat.Id.IntegerValue == self._door_cat)
        except Exception:
            return False


def pick_linked_doors():
    if not LINK_MAP:
        return []

    resolved = []
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.LinkedElement,
            LinkedDoorFilter(LINK_MAP),
            "Pick LINKED doors — Finish when done, ESC to skip"
        )
    except Exception:
        refs = []

    seen = set()
    for ref in refs:
        try:
            inst_id = ref.ElementId.IntegerValue
            if inst_id not in LINK_MAP:
                continue
            _, lnk_doc, xform = LINK_MAP[inst_id]
            linked_elem       = lnk_doc.GetElement(ref.LinkedElementId)
            if linked_elem is None:
                continue
            cat = linked_elem.Category
            if cat is None or cat.Id.IntegerValue != int(BuiltInCategory.OST_Doors):
                continue
            uid = (inst_id, ref.LinkedElementId.IntegerValue)
            if uid not in seen:
                seen.add(uid)
                label = "Link: {}".format(lnk_doc.Title)
                resolved.append((linked_elem, xform, label))
        except Exception:
            continue

    return resolved


# =========================================================
# MAIN
# =========================================================
output.print_md("# PWD Door Clearance Check")
output.print_md("Step 1: pick host doors.  Step 2: pick linked doors.")

host_doors   = pick_host_doors()
linked_doors = pick_linked_doors()
door_list    = host_doors + linked_doors

if not door_list:
    output.print_md("No doors selected — cancelled.")
    sys.exit()

output.print_md("**Doors:** {} host + {} linked  |  **View:** `{}`".format(
    len(host_doors), len(linked_doors), view.Name))

geo_options                          = Options()
geo_options.ComputeReferences        = False
geo_options.IncludeNonVisibleObjects = True

subcat_ids = get_subcat_style_ids(SUBCAT_NAME)
if not subcat_ids:
    alert(
        "Subcategory '" + SUBCAT_NAME + "' not found under Doors.\n\n"
        "Please create it in the door family, assign it to your "
        "clearance solid, then reload the family.",
        title="Missing Subcategory"
    )
    sys.exit()

output.print_md("**Subcategory:** `" + SUBCAT_NAME + "` found — proceeding.")
output.print_md("---")


# =========================================================
# TRANSACTION (host doors only — param write)
# Linked doors are report-only, no param change needed.
# =========================================================
t = Transaction(doc, "PWD Door Clearance Check")
t.Start()

try:
    total_pass = 0
    total_fail = 0
    no_param   = 0
    no_geo     = 0
    results    = []

    # ── Pass 1: force Show PWD Check = True on host doors ────────────
    valid_host_doors = []
    for elem, xform, source_label in host_doors:
        param = elem.LookupParameter(PARAM_NAME)
        if param is None:
            no_param += 1
            output.print_md("  Skip {} ({}) — missing parameter.".format(
                elem.Id.IntegerValue, source_label))
            continue
        if param.IsReadOnly:
            no_param += 1
            output.print_md("  Skip {} ({}) — parameter is read-only.".format(
                elem.Id.IntegerValue, source_label))
            continue
        param.Set(1)
        valid_host_doors.append((elem, xform, source_label, param))

    # Regenerate so Revit exports geometry with param = True
    doc.Regenerate()

    # ── Pass 2: host doors — geometry bbox + obstruction check ───────
    for elem, xform, source_label, param in valid_host_doors:

        link_xform   = None if xform.IsIdentity else xform
        host_wall_id = None
        try:
            host = elem.Host
            if host:
                host_wall_id = host.Id
        except Exception:
            pass

        bb = get_clearance_bbox_from_geometry(
            elem, subcat_ids, geo_options, link_xform)

        if bb is None:
            no_geo += 1
            output.print_md("  Skip {} ({}) — no PWD Clearance geometry found.".format(
                elem.Id.IntegerValue, source_label))
            continue

        obstructed = has_obstruction(bb, elem.Id, host_wall_id)
        param.Set(1 if obstructed else 0)

        status = "FAIL" if obstructed else "PASS"
        if obstructed:
            total_fail += 1
        else:
            total_pass += 1

        results.append((elem.Id.IntegerValue, source_label, status))

    t.Commit()

except Exception as ex:
    if t.HasStarted():
        t.RollBack()
    alert("Transaction failed:\n" + str(ex),
          title="PWD Check — Error")
    sys.exit()


# ── Linked doors — orientation bbox + obstruction check (no param) ──
lnk_pass = 0
lnk_fail = 0
lnk_skip = 0

for elem, xform, source_label in linked_doors:

    link_xform   = None if xform.IsIdentity else xform
    host_wall_id = None
    try:
        host = elem.Host
        if host:
            host_wall_id = host.Id
    except Exception:
        pass

    pull_bb, push_bb = get_clearance_bboxes_from_orientation(elem, link_xform)

    if pull_bb is None or push_bb is None:
        lnk_skip += 1
        output.print_md("  Skip {} ({}) — could not compute clearance zone.".format(
            elem.Id.IntegerValue, source_label))
        continue

    pull_obstructed = has_obstruction(pull_bb, elem.Id, host_wall_id)
    push_obstructed = has_obstruction(push_bb, elem.Id, host_wall_id)
    obstructed      = pull_obstructed or push_obstructed

    status = "FAIL" if obstructed else "PASS"
    if obstructed:
        lnk_fail += 1
    else:
        lnk_pass += 1

    side_note = ""
    if pull_obstructed and push_obstructed:
        side_note = " (both sides)"
    elif pull_obstructed:
        side_note = " (pull side)"
    elif push_obstructed:
        side_note = " (push side)"

    results.append((elem.Id.IntegerValue, source_label, status + side_note))


# =========================================================
# REPORT
# =========================================================
output.print_md("## Results")
output.print_md("| Door ID | Source | Result |")
output.print_md("|---|---|---|")
for did, src, res in results:
    output.print_md("| {} | {} | {} |".format(did, src, res))
output.print_md("---")
output.print_md("**Host — PASS: {}  |  FAIL: {}  |  No param: {}  |  No geometry: {}**".format(
    total_pass, total_fail, no_param, no_geo))
output.print_md("**Linked — PASS: {}  |  FAIL: {}  |  Skipped: {}**".format(
    lnk_pass, lnk_fail, lnk_skip))
output.print_md("")
output.print_md("> Host doors: Show PWD Check = True if obstructed, False if clear")
output.print_md("> Linked doors: report only, no parameter change (1500mm pull / 1200mm push)")