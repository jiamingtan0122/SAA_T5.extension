# -*- coding: utf-8 -*-
__title__ = "Room To\nBanded Ceiling"
__author__ = "JM"
__doc__ = """Version = 4.11
Date    = 24.08.2026
Description:
Generates a TWO-PART ceiling from selected Room(s):
  1) A perimeter "ring" ceiling (default type match: 'GWB-01') - everything
     that is NOT a full 600x600 tile.
  2) A middle ceiling (default type match: 'ACS-02') built ONLY from
     600x600 cells that fit ENTIRELY inside the room boundary.

This guarantees zero cut tiles: the room is rasterised into a 600x600
grid (primary axis = the picked wall's direction, grid PHASE anchored to
that wall's own start point - every room processed with the same picked
wall shares identical grid lines, so adjacent rooms/spaces line up
seamlessly, including where they're split by an invisible room
separation line rather than an actual wall). The picked wall's direction
is used for EVERY room in the run, even one whose own walls sit at a
different rotation - your wall pick is authoritative and is not
silently overridden. If a room's own walls genuinely don't line up with
the picked wall, expect more tiles to fail the clearance test near its
edges (a real geometric consequence of the mismatch, not a bug) - pick a
wall that belongs to that room if you want its grid self-contained
instead of following a neighbour's reference. Any cell that would be clipped by a wall - including a diagonal wall that a simple
perpendicular offset can't fully clear - is dropped and its area becomes
part of the GWB-01 ring instead. On top of that, every kept cell must
also have at least 300mm of clearance to the wall on each of its 4
sides - so if the natural leftover margin is thinner than that (e.g.
111mm), the whole outer row/column of tiles is dropped too, pushing the
margin up rather than leaving a too-thin sliver. The kept cells are
merged into one boundary, so the ring can end up wide in some spots
(e.g. a corner cut off by a diagonal wall) and narrow in others - that is
expected and matches the intent (all internal boxes fully 600x600, ring
generally 300-500mm, wider only where geometry forces it). If a room has
more than one disconnected pocket of full tiles (e.g. a small alcove
separate from the main field), EACH pocket gets its own ACS-02 ceiling -
not just the largest one.

COLUMNS: any interior obstruction the room boundary reports (typically a
room-bounding column) keeps its own footprint as a genuine hole in both
ceilings - no ceiling finish sits on top of the column, same as before.
On top of that, any 600x600 cell that TOUCHES or is cut by that column is
now also excluded from ACS-02, so the tiles immediately around a column
become part of the GWB-01 ring instead of a cut tile hugging the column.

ROOM-IN-ROOM: if two SELECTED rooms overlap - e.g. a small partition
room from one linked model sitting inside a big room from a different
linked model - the smaller room's footprint is automatically treated as
an extra hole in the bigger room's ceilings, so the bigger room's
GWB-01/ACS-02 doesn't overlap the smaller room's own independently
generated ceiling. Both rooms must be in the current selection for this
to trigger; it's detected by geometry (most of the smaller room's
boundary falling inside the bigger one), not by name or link source.

- Step 1: Select Room(s).
- Step 2: Pick a wall (host or linked, including curtain walls - click
  the panel or mullion and it resolves back to the parent wall) - its
  direction AND position set the tile grid for every room in this run,
  so pick the same wall across a run whenever adjacent rooms need to
  share one continuous grid.
- Step 3: Confirm/select GWB-01 (ring) and ACS-02 (middle) Ceiling Types.
- Step 4: Enter Height Offset (mm) - applied to both ceilings.

ASSUMPTION TO VERIFY: grid phase is anchored to the picked wall's own
start point (grid lines fall on exact multiples of 600mm from it), not
centred per room. If your actual reflected ceiling grid is set from a
different reference point, the kept-cell positions will be offset from
what's actually on the drawing.

LIMITATION: needs a room boundary made of straight (Line) segments - a
room with curved walls will be skipped and reported. Gaps between
boundary segments (common along curtain walls, one segment per mullion
bay) are now bridged automatically, not just at the loop's start/end.
"""

import math
from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, ElementId, RevitLinkInstance,
    SpatialElement, SpatialElementBoundaryOptions,
    SpatialElementBoundaryLocation, FilteredElementCollector,
    Ceiling, CeilingType, Level, Transaction, CurveLoop, Line, XYZ, Wall,
    FamilyInstance, Mullion
)
from Autodesk.Revit.UI.Selection import ObjectType, ISelectionFilter
from pyrevit import revit, forms, script

# ---------------- INITIALIZATION ----------------
doc = revit.doc
uidoc = revit.uidoc

TILE_MM = 600
MM_TO_FT = 1.0 / 304.8
TILE_FT = TILE_MM * MM_TO_FT
MIN_CLEARANCE_MM = 300
MIN_CLEARANCE_FT = MIN_CLEARANCE_MM * MM_TO_FT

GWB_KEYWORD = "GWB-01"
ACS_KEYWORD = "ACS-02"

# ---------------- HELPERS ----------------

def get_type_name_safe(element):
    param = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
    if param and param.HasValue:
        return param.AsString()
    try:
        return element.Name
    except:
        return "Unnamed Type"


def get_all_ceiling_types():
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


def find_ceiling_type(keyword, types_dict):
    matches = [name for name in types_dict if keyword.lower() in name.lower()]
    if len(matches) == 1:
        return types_dict[matches[0]]
    return None


def resolve_to_wall(elem):
    """Given a picked element, returns the Wall it represents - itself if
    it's already a Wall, or its host Wall if it's a curtain panel or
    mullion. Returns None if it's not wall-related at all."""
    if isinstance(elem, Wall):
        return elem
    try:
        if isinstance(elem, FamilyInstance):
            host = elem.Host
            if isinstance(host, Wall):
                return host
    except Exception:
        pass
    try:
        if isinstance(elem, Mullion):
            host = getattr(elem, "Host", None)
            if isinstance(host, Wall):
                return host
    except Exception:
        pass
    return None


class WallSelectionFilter(ISelectionFilter):
    """Allows picking a wall (including curtain walls - by their panel or
    mullion, which is usually what actually gets clicked) in the host
    model, or inside a link (via ObjectType.LinkedElement picking)."""

    def __init__(self, host_doc):
        self.host_doc = host_doc

    def AllowElement(self, element):
        return resolve_to_wall(element) is not None or isinstance(element, RevitLinkInstance)

    def AllowReference(self, reference, point):
        try:
            if reference.LinkedElementId != ElementId.InvalidElementId:
                link_inst = self.host_doc.GetElement(reference.ElementId)
                if isinstance(link_inst, RevitLinkInstance):
                    link_doc = link_inst.GetLinkDocument()
                    if link_doc:
                        elem = link_doc.GetElement(reference.LinkedElementId)
                        return resolve_to_wall(elem) is not None
                return False
            else:
                elem = self.host_doc.GetElement(reference.ElementId)
                return resolve_to_wall(elem) is not None
        except Exception:
            return False


def get_boundary_loops(room, transform=None):
    """
    Returns CurveLoops for room boundaries (Finish). Bridges gaps between
    ANY two consecutive segments - not just the final closing gap - since
    curtain walls report their boundary as one short segment per mullion
    bay/panel, and those segments sometimes don't quite meet (mullion
    reveal offsets etc.), which otherwise leaves the loop broken partway
    around instead of only at the start/end.
    """
    options = SpatialElementBoundaryOptions()
    options.SpatialElementBoundaryLocation = SpatialElementBoundaryLocation.Finish

    loops = []
    try:
        boundary_segments_list = room.GetBoundarySegments(options)
        if not boundary_segments_list:
            return None

        for segments in boundary_segments_list:
            raw_curves = []
            for seg in segments:
                curve = seg.GetCurve()
                if transform:
                    curve = curve.CreateTransformed(transform)
                raw_curves.append(curve)

            if not raw_curves:
                continue

            bridged_curves = []
            n = len(raw_curves)
            for idx in range(n):
                cur = raw_curves[idx]
                bridged_curves.append(cur)
                nxt = raw_curves[(idx + 1) % n]
                end_pt = cur.GetEndPoint(1)
                start_pt = nxt.GetEndPoint(0)
                if end_pt.DistanceTo(start_pt) > 0.003:  # ~1mm
                    try:
                        bridged_curves.append(Line.CreateBound(end_pt, start_pt))
                    except Exception as e:
                        print("Could not bridge boundary gap: {}".format(e))

            curve_loop = CurveLoop()
            for c in bridged_curves:
                try:
                    curve_loop.Append(c)
                except Exception as e:
                    print("Skipping bad curve while building boundary: {}".format(e))

            if curve_loop.IsOpen():
                # last-resort fallback: close first-to-last directly
                try:
                    count = 0
                    first_curve = None
                    last_curve = None
                    for c in curve_loop:
                        if count == 0:
                            first_curve = c
                        last_curve = c
                        count += 1
                    if first_curve and last_curve:
                        start_pt = first_curve.GetEndPoint(0)
                        end_pt = last_curve.GetEndPoint(1)
                        if start_pt.DistanceTo(end_pt) > 0.003:
                            curve_loop.Append(Line.CreateBound(end_pt, start_pt))
                except Exception as e:
                    print("Could not auto-close loop: {}".format(e))

            loops.append(curve_loop)
        return loops
    except:
        return None


def get_loop_polygon_points(curve_loop):
    """Ordered, tessellated (x, y) tuples describing the loop, dropping the
    duplicated closing point."""
    pts = []
    for curve in curve_loop:
        tess = list(curve.Tessellate())
        pts.extend(tess[:-1])
    return [(p.X, p.Y) for p in pts]


def get_loop_area(curve_loop):
    pts = get_loop_polygon_points(curve_loop)
    if len(pts) < 3:
        return 0.0
    area = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += (x1 * y2 - x2 * y1)
    return abs(area) * 0.5


def point_in_polygon(px, py, poly_xy):
    n = len(poly_xy)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = poly_xy[i]
        xj, yj = poly_xy[j]
        if ((yi > py) != (yj > py)):
            x_cross = (xj - xi) * (py - yi) / ((yj - yi) if (yj - yi) != 0 else 1e-12) + xi
            if px < x_cross:
                inside = not inside
        j = i
    return inside


def segments_intersect(p1, p2, p3, p4):
    def ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
    d1 = ccw(p3, p4, p1)
    d2 = ccw(p3, p4, p2)
    d3 = ccw(p1, p2, p3)
    d4 = ccw(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and \
       ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    return False


def polygons_overlap(poly_a_xy, poly_b_xy):
    """True if two simple polygons (as ordered (x,y) point lists) touch
    or overlap at all - covers full containment either way and boundary
    crossings, used to test a candidate cell against a column footprint."""
    for (x, y) in poly_a_xy:
        if point_in_polygon(x, y, poly_b_xy):
            return True
    for (x, y) in poly_b_xy:
        if point_in_polygon(x, y, poly_a_xy):
            return True
    edges_a = [(poly_a_xy[i], poly_a_xy[(i + 1) % len(poly_a_xy)]) for i in range(len(poly_a_xy))]
    edges_b = [(poly_b_xy[i], poly_b_xy[(i + 1) % len(poly_b_xy)]) for i in range(len(poly_b_xy))]
    for (a1, a2) in edges_a:
        for (b1, b2) in edges_b:
            if segments_intersect(a1, a2, b1, b2):
                return True
    return False


def polygon_mostly_inside(poly_a_xy, poly_b_xy, threshold=0.85):
    """
    True if at least `threshold` fraction of poly_a's vertices fall
    inside poly_b. Used to detect a smaller room's footprint nested
    inside a bigger room's footprint (room-in-room), e.g. a small
    partition room from one linked model sitting inside a big room from
    a different linked model. Vertex-based rather than exact-area-based
    on purpose, so it still works when the smaller room's boundary
    isn't perfectly flush with anything inside the bigger one.
    """
    if not poly_a_xy:
        return False
    inside = sum(1 for (x, y) in poly_a_xy if point_in_polygon(x, y, poly_b_xy))
    return inside >= max(1, int(math.ceil(threshold * len(poly_a_xy))))


def get_dominant_edge_direction(outer_loop):
    """
    This room's own natural grid axis, chosen by which direction has the
    most TOTAL wall length behind it - not just the single longest edge.
    A room that's a plain rectangle with one anomalously long diagonal
    segment (a real short angled feature, or a bridged gap from
    get_boundary_loops) should still resolve to the rectangle's own
    orthogonal direction, since that has far more total length across
    multiple edges than one diagonal.
    """
    line_edges = [e for e in outer_loop if isinstance(e, Line)]
    if not line_edges:
        return None

    buckets = {}  # 1-degree bucket (0-179) -> accumulated length
    for e in line_edges:
        try:
            d = e.GetEndPoint(1) - e.GetEndPoint(0)
            length = d.GetLength()
            if length < 1e-6:
                continue
            dn = d.Normalize()
            angle = math.degrees(math.atan2(dn.Y, dn.X)) % 180.0
            bucket = int(round(angle)) % 180
            buckets[bucket] = buckets.get(bucket, 0.0) + length
        except Exception:
            continue

    if not buckets:
        return None

    best_bucket = max(buckets, key=lambda b: buckets[b])
    rad = math.radians(best_bucket)
    return XYZ(math.cos(rad), math.sin(rad), 0)


def build_cell_grid_inner_loop(outer_loop, u_override=None, hole_loops=None, grid_anchor=None):
    """
    Rasterises the room into a 600x600 grid and keeps only cells that lie
    ENTIRELY inside the room, with a 300mm clearance floor on every side.
    Disconnected pockets of full cells (e.g. a small alcove separate from
    the main field) are each kept as their own region - not just the
    largest one.
    u_override: a normalized XYZ giving the grid's primary direction
    (from the wall picked at the start of the script). If None (should
    not normally happen), falls back to the room's own longest edge.
    grid_anchor: a fixed XYZ point (the picked wall's own start point)
    used as the grid's phase reference. When given, ALL rooms processed
    with the same picked wall share identical grid lines - this is what
    keeps two adjacent rooms/spaces (e.g. split by an invisible room
    separation line) seamless instead of each getting its own
    independently bounding-box-centred grid that doesn't line up with
    its neighbour. If None, falls back to centring the grid on this
    room's own bounding box (only safe for a single standalone room).
    hole_loops: interior obstruction loops (e.g. columns) reported by the
    room boundary. Any cell that touches or is cut by one of these is
    dropped from ACS-02 (it becomes GWB-01 ring by default) - the column
    footprint itself stays a genuine hole (no ceiling finish over it) in
    both ceilings, unchanged from before.
    Returns (list_of_inner_CurveLoops, stats_dict) or (None, None) on
    failure.
    """
    line_edges = [e for e in outer_loop if isinstance(e, Line)]
    if not line_edges:
        return None, None

    poly_xy = get_loop_polygon_points(outer_loop)
    if len(poly_xy) < 3:
        return None, None
    poly_edges_xy = [(poly_xy[i], poly_xy[(i + 1) % len(poly_xy)]) for i in range(len(poly_xy))]

    hole_polys_xy = []
    for hl in (hole_loops or []):
        h_pts = get_loop_polygon_points(hl)
        if len(h_pts) >= 3:
            hole_polys_xy.append(h_pts)

    if u_override is not None:
        U = u_override
        z0 = line_edges[0].GetEndPoint(0).Z
    else:
        U = get_dominant_edge_direction(outer_loop)
        if U is None:
            return None, None
        z0 = line_edges[0].GetEndPoint(0).Z
    V = XYZ(-U.Y, U.X, 0)

    if grid_anchor is not None:
        P0 = XYZ(grid_anchor.X, grid_anchor.Y, z0)
    else:
        P0 = XYZ(poly_xy[0][0], poly_xy[0][1], z0)

    def to_uv(x, y):
        rel = XYZ(x, y, z0) - P0
        return rel.DotProduct(U), rel.DotProduct(V)

    def uv_to_xy(u, v):
        pt = P0 + U.Multiply(u) + V.Multiply(v)
        return (pt.X, pt.Y)

    us, vs = [], []
    for (x, y) in poly_xy:
        u, v = to_uv(x, y)
        us.append(u)
        vs.append(v)
    min_u, max_u = min(us), max(us)
    min_v, max_v = min(vs), max(vs)
    if grid_anchor is not None:
        # Grid lines pass exactly through the shared anchor point (every
        # multiple of 600mm from it) - identical for every room that
        # used this same picked wall, so adjacent rooms stay in phase.
        origin_u = 0.0
        origin_v = 0.0
    else:
        origin_u = (min_u + max_u) / 2.0
        origin_v = (min_v + max_v) / 2.0

    i_min = int(math.floor((min_u - origin_u) / TILE_FT)) - 1
    i_max = int(math.ceil((max_u - origin_u) / TILE_FT)) + 1
    j_min = int(math.floor((min_v - origin_v) / TILE_FT)) - 1
    j_max = int(math.ceil((max_v - origin_v) / TILE_FT)) + 1

    kept_cells = set()
    for i in range(i_min, i_max):
        for j in range(j_min, j_max):
            u0 = origin_u + i * TILE_FT
            u1 = u0 + TILE_FT
            v0 = origin_v + j * TILE_FT
            v1 = v0 + TILE_FT
            corners_uv = [(u0, v0), (u1, v0), (u1, v1), (u0, v1)]
            cu = (u0 + u1) / 2.0
            cv = (v0 + v1) / 2.0

            corners_xy = []
            ok = True
            for (u, v) in corners_uv:
                # nudge corner a hair toward the cell centre to dodge
                # floating-point edge-on-boundary ambiguity
                nu = u + (cu - u) * 0.0008
                nv = v + (cv - v) * 0.0008
                x, y = uv_to_xy(nu, nv)
                corners_xy.append(uv_to_xy(u, v))
                if not point_in_polygon(x, y, poly_xy):
                    ok = False
            if not ok:
                continue

            crossed = False
            for k in range(4):
                x1, y1 = corners_xy[k]
                x2, y2 = corners_xy[(k + 1) % 4]
                for (p3, p4) in poly_edges_xy:
                    if segments_intersect((x1, y1), (x2, y2), p3, p4):
                        crossed = True
                        break
                if crossed:
                    break
            if crossed:
                continue

            # Minimum-clearance floor: each of the 4 edges must have at
            # least MIN_CLEARANCE_FT of room between it and the wall,
            # tested perpendicular to that edge. This is what drops a
            # whole outer row/column when the natural leftover is too
            # thin (e.g. 111mm), pushing the remaining margin up
            # (typically toward 300-500mm on a centred grid).
            clearance_ok = True
            edge_pushes = [
                ((u0 + u1) / 2.0, v0 - MIN_CLEARANCE_FT),  # bottom, push -V
                ((u0 + u1) / 2.0, v1 + MIN_CLEARANCE_FT),  # top, push +V
                (u0 - MIN_CLEARANCE_FT, (v0 + v1) / 2.0),  # left, push -U
                (u1 + MIN_CLEARANCE_FT, (v0 + v1) / 2.0),  # right, push +U
            ]
            for (pu, pv) in edge_pushes:
                px, py = uv_to_xy(pu, pv)
                if not point_in_polygon(px, py, poly_xy):
                    clearance_ok = False
                    break
            if not clearance_ok:
                continue

            column_hit = False
            for hole_xy in hole_polys_xy:
                if polygons_overlap(corners_xy, hole_xy):
                    column_hit = True
                    break
            if column_hit:
                continue

            kept_cells.add((i, j))

    if not kept_cells:
        return None, None

    edge_count = {}

    def add_edge(a, b):
        key = (a, b) if a <= b else (b, a)
        edge_count[key] = edge_count.get(key, 0) + 1

    for (i, j) in kept_cells:
        c00, c10, c11, c01 = (i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1)
        add_edge(c00, c10)
        add_edge(c10, c11)
        add_edge(c11, c01)
        add_edge(c01, c00)

    boundary_edges = [e for e, cnt in edge_count.items() if cnt == 1]
    if not boundary_edges:
        return None, None

    adj = {}
    for (a, b) in boundary_edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)

    visited = set()
    loops_grid = []
    for (a, b) in boundary_edges:
        key0 = frozenset([a, b])
        if key0 in visited:
            continue
        loop = [a, b]
        visited.add(key0)
        prev, current = a, b
        safety = 0
        while current != a and safety < 100000:
            safety += 1
            neighbors = adj.get(current, [])
            nxt = None
            for cand in neighbors:
                ek = frozenset([current, cand])
                if ek in visited:
                    continue
                nxt = cand
                break
            if nxt is None:
                break
            loop.append(nxt)
            visited.add(frozenset([current, nxt]))
            prev, current = current, nxt
        if len(loop) >= 5 and loop[0] == loop[-1]:
            loops_grid.append(loop[:-1])

    if not loops_grid:
        return None, None

    def grid_loop_area(loop):
        pts = [uv_to_xy(origin_u + ii * TILE_FT, origin_v + jj * TILE_FT) for (ii, jj) in loop]
        n = len(pts)
        a = 0.0
        for k in range(n):
            x1, y1 = pts[k]
            x2, y2 = pts[(k + 1) % n]
            a += x1 * y2 - x2 * y1
        return abs(a) / 2.0

    inner_loops = []
    for grid_loop in loops_grid:
        xy_pts = [uv_to_xy(origin_u + ii * TILE_FT, origin_v + jj * TILE_FT) for (ii, jj) in grid_loop]
        xyz_pts = [XYZ(x, y, z0) for (x, y) in xy_pts]

        simplified = []
        n = len(xyz_pts)
        for idx in range(n):
            prev_pt = xyz_pts[idx - 1]
            cur_pt = xyz_pts[idx]
            next_pt = xyz_pts[(idx + 1) % n]
            v1x, v1y = cur_pt.X - prev_pt.X, cur_pt.Y - prev_pt.Y
            v2x, v2y = next_pt.X - cur_pt.X, next_pt.Y - cur_pt.Y
            cross = v1x * v2y - v1y * v2x
            if abs(cross) > 1e-9:
                simplified.append(cur_pt)
        if len(simplified) < 4:
            simplified = xyz_pts

        try:
            one_loop = CurveLoop()
            m = len(simplified)
            for idx in range(m):
                p1 = simplified[idx]
                p2 = simplified[(idx + 1) % m]
                if p1.DistanceTo(p2) > 0.003:
                    one_loop.Append(Line.CreateBound(p1, p2))
            if one_loop.IsOpen():
                continue
            inner_loops.append(one_loop)
        except Exception:
            continue

    if not inner_loops:
        return None, None

    stats = {"cell_count": len(kept_cells), "region_count": len(inner_loops)}
    return inner_loops, stats


def create_ceiling_safe(curve_loops, ceiling_type_id, level_id, height_offset_feet):
    try:
        new_ceiling = Ceiling.Create(doc, curve_loops, ceiling_type_id, level_id)
        if new_ceiling:
            param = new_ceiling.get_Parameter(BuiltInParameter.CEILING_HEIGHTABOVELEVEL_PARAM)
            if param:
                param.Set(height_offset_feet)
        return new_ceiling, None
    except Exception as e:
        return None, str(e)


# ---------------- SELECTION VALIDATION ----------------

sel_refs = uidoc.Selection.GetReferences()
if not sel_refs:
    forms.alert("Please select Room(s) first.", exitscript=True)

# ---------------- GRID ALIGNMENT ----------------
# The 600x600 grid is aligned to a wall you pick - host or linked.

u_override = None
grid_anchor = None
try:
    wall_ref = uidoc.Selection.PickObject(
        ObjectType.LinkedElement, WallSelectionFilter(doc),
        "Select a wall (host or linked) to align the tile grid direction"
    )
    if wall_ref.LinkedElementId != ElementId.InvalidElementId:
        link_inst = doc.GetElement(wall_ref.ElementId)
        link_doc = link_inst.GetLinkDocument()
        picked_elem = link_doc.GetElement(wall_ref.LinkedElementId) if link_doc else None
        link_transform = link_inst.GetTotalTransform()
    else:
        picked_elem = doc.GetElement(wall_ref.ElementId)
        link_transform = None

    wall_elem = resolve_to_wall(picked_elem) if picked_elem is not None else None

    wall_curve = None
    if wall_elem is not None:
        loc = wall_elem.Location
        wall_curve = getattr(loc, "Curve", None)

    if wall_curve and isinstance(wall_curve, Line):
        if link_transform:
            wall_curve = wall_curve.CreateTransformed(link_transform)
        u_override = (wall_curve.GetEndPoint(1) - wall_curve.GetEndPoint(0)).Normalize()
        # Anchor the grid's phase to the wall's own start point (not each
        # room's own bounding box) so every room picked with this same
        # wall shares IDENTICAL grid lines - this is what keeps adjacent
        # rooms/spaces seamless instead of each getting its own
        # independently-centred (and therefore out-of-phase) grid.
        grid_anchor = wall_curve.GetEndPoint(0)
    else:
        forms.alert("Selected wall isn't straight (or has no line location) - cannot align to it.", exitscript=True)
except Exception as e:
    forms.alert("Wall pick cancelled or failed: {}".format(e), exitscript=True)

# ---------------- GATHER CEILING TYPES ----------------

all_ceiling_types = get_all_ceiling_types()
ceiling_types_dict = {}
for ct in all_ceiling_types:
    t_name = get_type_name_safe(ct)
    if t_name:
        ceiling_types_dict[t_name] = ct

if not ceiling_types_dict:
    forms.alert("No Ceiling Types found in the project.", exitscript=True)

all_levels = get_all_levels()
levels_dict = {l.Name: l for l in all_levels}

# ---------------- USER INPUTS ----------------

gwb_type = find_ceiling_type(GWB_KEYWORD, ceiling_types_dict)
if not gwb_type:
    gwb_name = forms.SelectFromList.show(
        sorted(ceiling_types_dict.keys()),
        title="Select PERIMETER (ring) Ceiling Type - e.g. GWB-01",
        multiselect=False
    )
    if not gwb_name:
        script.exit()
    gwb_type = ceiling_types_dict[gwb_name]

acs_type = find_ceiling_type(ACS_KEYWORD, ceiling_types_dict)
if not acs_type:
    acs_name = forms.SelectFromList.show(
        sorted(ceiling_types_dict.keys()),
        title="Select MIDDLE (tile) Ceiling Type - e.g. ACS-02",
        multiselect=False
    )
    if not acs_name:
        script.exit()
    acs_type = ceiling_types_dict[acs_name]

offset_str = forms.ask_for_string(
    default="3000",
    prompt="Enter Height Offset (mm) - applied to both ceilings:",
    title="Ceiling Height"
)
if offset_str is None:
    script.exit()

try:
    val_mm = float(offset_str)
    height_offset_feet = val_mm / 304.8
except ValueError:
    forms.alert("Invalid number entered.", exitscript=True)

# ---------------- PROCESSING ----------------

ceilings_created = 0
rooms_skipped = []
rooms_flagged = []
errors = []
fallback_level_map = {}

t = Transaction(doc, "Create Banded Room Ceilings (GWB-01 + ACS-02)")
t.Start()

# --- Pass 1: resolve every selected room's own geometry first (before
# building any ceiling) - needed so Pass 2 can compare ALL selected
# rooms against each other for room-in-room overlap, e.g. a small
# partition room from one linked model sitting inside a big room from a
# different linked model.
room_data_list = []
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

    if room_element and isinstance(room_element, SpatialElement):
        if room_element.Category and room_element.Category.Id.IntegerValue == int(BuiltInCategory.OST_Rooms):

            all_loops = get_boundary_loops(room_element, transform)
            if not all_loops or not target_level_id:
                rooms_skipped.append("Room {}: no boundary or level found".format(room_element.Id))
                continue

            loop_areas = [(lp, get_loop_area(lp)) for lp in all_loops]
            loop_areas.sort(key=lambda x: x[1], reverse=True)
            outer_loop = loop_areas[0][0]
            own_hole_loops = [lp for lp, _ in loop_areas[1:]]

            room_data_list.append({
                "room_element": room_element,
                "outer_loop": outer_loop,
                "hole_loops": list(own_hole_loops),
                "target_level_id": target_level_id,
                "outer_poly_xy": get_loop_polygon_points(outer_loop),
                "area": get_loop_area(outer_loop),
            })

# --- Pass 2: detect room-in-room overlap across ALL selected rooms
# (loops are already in host coordinates regardless of which link a
# room came from, so this works across links). If a smaller room's
# footprint is mostly nested inside a bigger room's footprint, treat the
# smaller room's boundary as an extra hole for the bigger one, so the
# bigger room's ceiling doesn't overlap the smaller partition room's own
# independently-generated ceiling.
room_in_room_notes = []
for i in range(len(room_data_list)):
    for j in range(len(room_data_list)):
        if i == j:
            continue
        big = room_data_list[i]
        small = room_data_list[j]
        if small["area"] >= big["area"]:
            continue
        if polygon_mostly_inside(small["outer_poly_xy"], big["outer_poly_xy"]):
            big["hole_loops"].append(small["outer_loop"])
            room_in_room_notes.append(
                "Room {} treated as a room-in-room cutout inside Room {}".format(
                    small["room_element"].Id, big["room_element"].Id
                )
            )

# --- Pass 3: build and create the ceilings.
for rd in room_data_list:
    room_element = rd["room_element"]
    outer_loop = rd["outer_loop"]
    hole_loops = rd["hole_loops"]
    target_level_id = rd["target_level_id"]

    inner_loops, stats = build_cell_grid_inner_loop(
        outer_loop, u_override=u_override, hole_loops=hole_loops, grid_anchor=grid_anchor
    )

    if not inner_loops:
        rooms_skipped.append(
            "Room {}: could not fit any full 600x600 tile (room too small/narrow, "
            "or has curved walls) - create this ceiling manually".format(room_element.Id)
        )
        continue

    outer_area = get_loop_area(outer_loop)
    valid_inner_loops = []
    for lp in inner_loops:
        a = get_loop_area(lp)
        if a > 0 and a < outer_area:
            valid_inner_loops.append(lp)
    if not valid_inner_loops:
        rooms_skipped.append(
            "Room {}: computed inner boundary invalid - create this ceiling manually".format(room_element.Id)
        )
        continue

    note = "Room {}: {} full 600x600 tile(s) kept across {} pocket(s), all cuts absorbed into the GWB-01 ring".format(
        room_element.Id, stats["cell_count"], len(valid_inner_loops)
    )
    rooms_flagged.append(note)

    band_loops = [outer_loop] + valid_inner_loops + hole_loops
    band_ceiling, band_err = create_ceiling_safe(
        band_loops, gwb_type.Id, target_level_id, height_offset_feet
    )
    if band_err:
        errors.append("Room {} (ring): {}".format(room_element.Id, band_err))
    if band_ceiling:
        ceilings_created += 1

    for region_idx, one_inner_loop in enumerate(valid_inner_loops):
        middle_loops = [one_inner_loop] + hole_loops
        middle_ceiling, middle_err = create_ceiling_safe(
            middle_loops, acs_type.Id, target_level_id, height_offset_feet
        )
        if middle_err:
            middle_ceiling, middle_err = create_ceiling_safe(
                [one_inner_loop], acs_type.Id, target_level_id, height_offset_feet
            )
            if middle_err:
                errors.append("Room {} (middle #{}): {}".format(
                    room_element.Id, region_idx + 1, middle_err
                ))
        if middle_ceiling:
            ceilings_created += 1

t.Commit()

# ---------------- OUTPUT ----------------
if ceilings_created == 0:
    msg = "No ceilings were created."
else:
    msg = "{} ceiling element(s) created.".format(ceilings_created)

if rooms_flagged:
    msg += "\n\nDetails:\n" + "\n".join(rooms_flagged[:10])
if room_in_room_notes:
    msg += "\n\nRoom-in-room:\n" + "\n".join(room_in_room_notes[:10])
if rooms_skipped:
    msg += "\n\nSkipped:\n" + "\n".join(rooms_skipped[:5])
if errors:
    msg += "\n\nErrors:\n" + "\n".join(errors[:5])

forms.alert(msg, title="Result", warn_icon=bool(rooms_skipped or errors))