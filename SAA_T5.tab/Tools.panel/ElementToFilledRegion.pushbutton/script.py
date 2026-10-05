# -*- coding: utf-8 -*-
__title__ = "Element To\nFilled Region"
__author__ = "JM"
__doc__ = """Version = 2.0
Date    = 2026-07-29

Description:
Create a Filled Region matching the boundary of whatever element(s) are
selected.

Uses your current selection (or lets you pick elements if nothing is
selected yet), works out a boundary for each one, and draws a Filled
Region on that boundary in the active view:

- Room       -> exact room boundary (all boundary loops, including
                any interior islands, via GetBoundarySegments).
- Wall       -> rectangular footprint from the wall's location line
                and width (straight walls only).
- Anything   -> exact boundary taken from the element's own solid
  else         geometry: the largest horizontal (plan-facing) face is
               found and ALL of its edge loops are used, so curved
               edges, notches, and interior holes come through exactly
               as modeled - not squared off. Falls back to the plan
               bounding box rectangle only if no usable solid face is
               found.

You then pick which Filled Region Type to use once, and it's applied
to every selected element. The active view must be a view type that
can host Filled Regions (plan, section, elevation, drafting, or
detail view).
"""

import traceback

from Autodesk.Revit.DB import (
    BooleanOperationsUtils,
    BooleanOperationsType,
    BuiltInCategory,
    CurveLoop,
    Element,
    FilledRegion,
    FilledRegionType,
    FilteredElementCollector,
    GeometryCreationUtilities,
    GeometryInstance,
    Line,
    Mesh,
    Options,
    Solid,
    SpatialElementBoundaryOptions,
    Transaction,
    UV,
    ViewDetailLevel,
    XYZ,
)
from Autodesk.Revit.UI import TaskDialog, TaskDialogIcon
from Autodesk.Revit.UI.Selection import ObjectType

from pyrevit import forms, revit, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# ~0.2 mm in feet - used to de-duplicate tessellated points.
POINT_TOL = 0.0007


def safe_str(value, fallback=""):
    if value is None:
        return fallback
    try:
        return unicode(value)
    except NameError:
        try:
            return str(value)
        except Exception:
            return fallback


def element_name(element):
    try:
        return safe_str(Element.Name.GetValue(element), "")
    except Exception:
        try:
            return safe_str(element.Name, "")
        except Exception:
            return ""


def category_id_matches(element, built_in_category):
    try:
        return (
            element is not None
            and element.Category is not None
            and element.Category.Id.IntegerValue == int(built_in_category)
        )
    except Exception:
        return False


def is_room(element):
    return category_id_matches(element, BuiltInCategory.OST_Rooms)


def is_wall(element):
    return category_id_matches(element, BuiltInCategory.OST_Walls)


def show_info(instruction, content=None, icon=TaskDialogIcon.TaskDialogIconInformation):
    dialog = TaskDialog("Create Filled Region")
    dialog.MainIcon = icon
    dialog.MainInstruction = instruction
    if content:
        dialog.MainContent = content
    dialog.Show()


def get_selected_elements():
    selected_ids = list(uidoc.Selection.GetElementIds())
    if selected_ids:
        elements = [doc.GetElement(eid) for eid in selected_ids]
        return [e for e in elements if e is not None]

    try:
        references = uidoc.Selection.PickObjects(
            ObjectType.Element,
            "Select elements to build filled region boundaries from, then click Finish.",
        )
    except Exception:
        # User pressed Esc / cancelled the pick.
        return None

    elements = [doc.GetElement(r.ElementId) for r in references]
    return [e for e in elements if e is not None]


def get_room_curve_loops(room):
    options = SpatialElementBoundaryOptions()
    loops = []
    segment_lists = room.GetBoundarySegments(options)
    for segments in segment_lists:
        if not segments:
            continue
        loop = CurveLoop()
        for segment in segments:
            loop.Append(segment.GetCurve())
        loops.append(loop)
    return loops


def get_wall_curve_loop(wall):
    location = wall.Location
    curve = getattr(location, "Curve", None)
    if curve is None or not isinstance(curve, Line):
        return None

    width = wall.Width
    half = width / 2.0
    direction = curve.Direction
    normal = XYZ(-direction.Y, direction.X, 0).Normalize()

    p0 = curve.GetEndPoint(0)
    p1 = curve.GetEndPoint(1)
    a = p0 + normal * half
    b = p1 + normal * half
    c = p1 - normal * half
    d = p0 - normal * half

    loop = CurveLoop()
    loop.Append(Line.CreateBound(a, b))
    loop.Append(Line.CreateBound(b, c))
    loop.Append(Line.CreateBound(c, d))
    loop.Append(Line.CreateBound(d, a))
    return [loop]


def collect_solids_and_meshes(geometry_element, solids, meshes):
    # Walks the element's geometry, recursing into GeometryInstance
    # (family/nested geometry), and buckets everything found into Solids
    # and Meshes. Imported/Rhino-derived DirectShapes often come through
    # as Mesh rather than Solid, which is why both need handling.
    for obj in geometry_element:
        try:
            if isinstance(obj, Solid):
                if obj.Volume > 0:
                    solids.append(obj)
            elif isinstance(obj, Mesh):
                meshes.append(obj)
            elif isinstance(obj, GeometryInstance):
                try:
                    instance_geometry = obj.GetInstanceGeometry()
                except Exception:
                    instance_geometry = None
                if instance_geometry is not None:
                    collect_solids_and_meshes(instance_geometry, solids, meshes)
        except Exception:
            continue


def get_horizontal_face_loops(solids):
    # First choice: a genuinely horizontal (plan-facing) face. Its edge
    # loops are used AS-IS, so arcs/splines stay exact rather than being
    # approximated - this is what preserves a curved edge precisely.
    best_face = None
    best_area = -1.0
    for solid in solids:
        for face in solid.Faces:
            try:
                normal = face.ComputeNormal(UV(0.5, 0.5))
            except Exception:
                continue
            if abs(abs(normal.Z) - 1.0) > 0.01:
                continue
            try:
                area = face.Area
            except Exception:
                continue
            if area > best_area:
                best_area = area
                best_face = face

    if best_face is None:
        return None

    try:
        curve_loop_array = best_face.GetEdgesAsCurveLoops()
    except Exception:
        return None

    loops = [loop for loop in curve_loop_array]
    return loops if loops else None


def best_projected_face(solids):
    # Fallback face choice for elements with no flat horizontal face at
    # all - e.g. a sloped roof or a freeform mass. Scores every planar
    # face by how much plan-view area it represents (face area weighted
    # by how vertical its normal is), and keeps the best one. This is
    # the face whose outline most closely traces the element's footprint
    # even though the face itself is tilted.
    best_face = None
    best_score = -1.0
    for solid in solids:
        for face in solid.Faces:
            try:
                normal = face.ComputeNormal(UV(0.5, 0.5))
                area = face.Area
            except Exception:
                continue
            score = area * abs(normal.Z)
            if score > best_score:
                best_score = score
                best_face = face
    return best_face


def tessellate_and_flatten_loops(curve_loop_array, target_z):
    # Converts a face's (possibly tilted, possibly curved) edge loops
    # into flat, view-plane-safe polygons: every curve is tessellated
    # into points, Z is dropped to a single constant elevation, and the
    # loop is rebuilt from straight segments between those points. This
    # is an approximation for curved/tilted edges, but it is what lets a
    # sloped roof or organic mass still produce a matching plan outline
    # instead of failing outright or falling back to a rectangle.
    loops = []
    for curve_loop in curve_loop_array:
        points = []
        for curve in curve_loop:
            try:
                tessellated = list(curve.Tessellate())
            except Exception:
                tessellated = [curve.GetEndPoint(0), curve.GetEndPoint(1)]
            for point in tessellated:
                flat_point = XYZ(point.X, point.Y, target_z)
                if not points or points[-1].DistanceTo(flat_point) > POINT_TOL:
                    points.append(flat_point)

        if len(points) < 3:
            continue
        if points[0].DistanceTo(points[-1]) > POINT_TOL:
            points.append(points[0])

        loop = CurveLoop()
        segment_count = 0
        for i in range(len(points) - 1):
            p0 = points[i]
            p1 = points[i + 1]
            if p0.DistanceTo(p1) < POINT_TOL:
                continue
            try:
                loop.Append(Line.CreateBound(p0, p1))
                segment_count += 1
            except Exception:
                continue

        if segment_count >= 3:
            loops.append(loop)

    return loops if loops else None


def get_tilted_face_loops(solids, view):
    face = best_projected_face(solids)
    if face is None:
        return None

    try:
        curve_loop_array = face.GetEdgesAsCurveLoops()
    except Exception:
        return None

    try:
        target_z = face.Origin.Z
    except Exception:
        target_z = 0.0

    return tessellate_and_flatten_loops(curve_loop_array, target_z)


def mesh_footprint_solid(meshes):
    # Rebuilds an exact footprint solid from raw triangulated mesh
    # geometry (typical of Rhino-imported DirectShapes) by extruding
    # every triangle a tiny height and Boolean-unioning them together.
    # The union recovers the true silhouette - including concave
    # notches and interior holes - which a convex hull or bounding box
    # cannot.
    combined_solid = None
    extrusion_height = 0.01  # ~3 mm in feet - just enough for a valid solid.

    for mesh in meshes:
        try:
            triangle_count = mesh.NumTriangles
        except Exception:
            continue

        for i in range(triangle_count):
            try:
                triangle = mesh.get_Triangle(i)
                p0 = triangle.get_Vertex(0)
                p1 = triangle.get_Vertex(1)
                p2 = triangle.get_Vertex(2)
            except Exception:
                continue

            if (
                p0.DistanceTo(p1) < POINT_TOL
                or p1.DistanceTo(p2) < POINT_TOL
                or p0.DistanceTo(p2) < POINT_TOL
            ):
                # Skip degenerate/sliver triangles.
                continue

            try:
                triangle_loop = CurveLoop()
                triangle_loop.Append(Line.CreateBound(p0, p1))
                triangle_loop.Append(Line.CreateBound(p1, p2))
                triangle_loop.Append(Line.CreateBound(p2, p0))
                triangle_solid = GeometryCreationUtilities.CreateExtrusionGeometry(
                    [triangle_loop], XYZ.BasisZ, extrusion_height
                )
            except Exception:
                continue

            if combined_solid is None:
                combined_solid = triangle_solid
                continue

            try:
                combined_solid = BooleanOperationsUtils.ExecuteBooleanOperation(
                    combined_solid, triangle_solid, BooleanOperationsType.Union
                )
            except Exception:
                # A sliver triangle failing to union shouldn't sink the
                # whole footprint - just skip it and keep going.
                continue

    return combined_solid


def get_mesh_face_loops(meshes, view):
    combined_solid = mesh_footprint_solid(meshes)
    if combined_solid is None:
        return None

    loops = get_horizontal_face_loops([combined_solid])
    if loops:
        return loops

    return get_tilted_face_loops([combined_solid], view)


def get_face_curve_loops(element, view):
    # Exact boundary via the element's actual geometry - this is what
    # makes curved edges, notches, and other non-rectangular shapes come
    # through precisely instead of being squared off to a bounding box.
    # Tries, in order: a flat horizontal face (exact curves preserved),
    # a tilted/sloped planar face flattened to a plan outline (covers
    # sloped roofs and freeform masses), then a footprint rebuilt from
    # raw mesh geometry (covers Rhino-imported DirectShapes with no
    # Solid at all).
    options = Options()
    options.ComputeReferences = False
    options.IncludeNonVisibleObjects = False
    try:
        options.DetailLevel = ViewDetailLevel.Fine
    except Exception:
        pass

    try:
        geometry = element.get_Geometry(options)
    except Exception:
        geometry = None
    if geometry is None:
        return None

    solids = []
    meshes = []
    collect_solids_and_meshes(geometry, solids, meshes)

    if solids:
        loops = get_horizontal_face_loops(solids)
        if loops:
            return loops

        loops = get_tilted_face_loops(solids, view)
        if loops:
            return loops

    if meshes:
        loops = get_mesh_face_loops(meshes, view)
        if loops:
            return loops

    return None


def get_bbox_curve_loop(element, view):
    bbox = None
    try:
        bbox = element.get_BoundingBox(view)
    except Exception:
        bbox = None
    if bbox is None:
        try:
            bbox = element.get_BoundingBox(None)
        except Exception:
            bbox = None
    if bbox is None:
        return None

    min_pt = bbox.Min
    max_pt = bbox.Max
    z = min_pt.Z
    a = XYZ(min_pt.X, min_pt.Y, z)
    b = XYZ(max_pt.X, min_pt.Y, z)
    c = XYZ(max_pt.X, max_pt.Y, z)
    d = XYZ(min_pt.X, max_pt.Y, z)

    loop = CurveLoop()
    loop.Append(Line.CreateBound(a, b))
    loop.Append(Line.CreateBound(b, c))
    loop.Append(Line.CreateBound(c, d))
    loop.Append(Line.CreateBound(d, a))
    return [loop]


def get_curve_loops_for_element(element, view):
    if is_room(element):
        try:
            loops = get_room_curve_loops(element)
            if loops:
                return loops, "Room boundary"
        except Exception:
            pass

    if is_wall(element):
        try:
            loops = get_wall_curve_loop(element)
            if loops:
                return loops, "Wall footprint"
        except Exception:
            pass

    try:
        loops = get_face_curve_loops(element, view)
        if loops:
            return loops, "Exact geometry boundary"
    except Exception:
        pass

    try:
        loops = get_bbox_curve_loop(element, view)
        if loops:
            return loops, "Bounding box"
    except Exception:
        pass

    return None, None


def prompt_filled_region_type():
    types = list(FilteredElementCollector(doc).OfClass(FilledRegionType))
    if not types:
        show_info("No Filled Region Types were found in this project.")
        return None

    name_to_type = {}
    for filled_region_type in types:
        name = element_name(filled_region_type)
        if name:
            name_to_type[name] = filled_region_type

    if not name_to_type:
        show_info("No named Filled Region Types were found in this project.")
        return None

    names = sorted(name_to_type.keys())
    selected_name = forms.SelectFromList.show(
        names,
        title="Create Filled Region",
        button_name="Select Filled Region Type",
        multiselect=False,
    )
    if selected_name is None:
        return None

    return name_to_type[selected_name]


def main():
    elements = get_selected_elements()
    if not elements:
        return

    view = doc.ActiveView
    if view is None:
        show_info("No active view was found.")
        return

    filled_region_type = prompt_filled_region_type()
    if filled_region_type is None:
        return

    results = []
    skipped = []

    transaction = Transaction(doc, "Create Filled Region From Boundary")
    transaction.Start()

    try:
        for element in elements:
            loops, source = get_curve_loops_for_element(element, view)
            if not loops:
                skipped.append((element, "No boundary geometry could be determined"))
                continue

            try:
                region = FilledRegion.Create(doc, filled_region_type.Id, view.Id, loops)
                results.append((element, source, region))
            except Exception:
                skipped.append((element, safe_str(traceback.format_exc())))

        transaction.Commit()
    except Exception:
        transaction.RollBack()
        show_info(
            "Create Filled Region failed.",
            safe_str(traceback.format_exc()),
            icon=TaskDialogIcon.TaskDialogIconError,
        )
        return

    output.print_md("# Create Filled Region From Boundary")
    output.print_md(
        "View: **{0}** | Filled Region Type: **{1}** | Elements Selected: **{2}**".format(
            element_name(view), element_name(filled_region_type), len(elements)
        )
    )

    if results:
        output.print_md("\n## Created ({0})".format(len(results)))
        output.print_table(
            table_data=[
                [
                    output.linkify(element.Id),
                    element.Category.Name if element.Category else "",
                    element_name(element),
                    source,
                    output.linkify(region.Id),
                ]
                for element, source, region in results
            ],
            columns=["Source Element Id", "Category", "Name", "Boundary Source", "New Filled Region Id"],
        )

    if skipped:
        output.print_md("\n## Skipped ({0})".format(len(skipped)))
        output.print_table(
            table_data=[
                [
                    output.linkify(element.Id),
                    element.Category.Name if element.Category else "",
                    element_name(element),
                    reason,
                ]
                for element, reason in skipped
            ],
            columns=["Element Id", "Category", "Name", "Reason"],
        )

    show_info(
        "Create Filled Region complete.",
        "{0} filled region(s) created, {1} element(s) skipped.".format(len(results), len(skipped)),
    )


if __name__ == "__main__":
    main()