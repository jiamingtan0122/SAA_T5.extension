# -*- coding: utf-8 -*-
"""Copy Callouts as Reference
 
Select one or more callouts in the active view. For each one a new reference
callout is created that matches how the original was drawn (Rectangle or
Sketch) and its rotation, uses the first type in CALLOUT_TYPE_NAMES that
Revit accepts, and points at the view named in TARGET_VIEW_NAME (Revit 2025).
After picking, you choose once whether to keep or delete the originals.
"""
 
__title__ = "Callout\nTo Fix"
__author__ = "Yuhan Huang - KPF Design Technology"
 
import math
import traceback
 
from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    CurveLoop,
    Element,
    ElementId,
    ElementTransformUtils,
    FilteredElementCollector,
    Line,
    SubTransaction,
    Transaction,
    Transform,
    View,
    ViewFamilyType,
    ViewSection,
    ViewType,
    XYZ,
)
from Autodesk.Revit.Exceptions import ArgumentException, OperationCanceledException
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType
from System.Collections.Generic import List
 
from pyrevit import revit, forms, script
 
doc = revit.doc
uidoc = revit.uidoc
 
# ---------------------------------------------------------------------------
# HARD-CODED TARGET: name of the view the new reference callouts will point to.
# Must be a Drafting view, or a cropped plan/section/detail view.
# ---------------------------------------------------------------------------
TARGET_VIEW_NAME = "ViewAQC Link- DO NOT DELETE"
 
# View types to try for the new callout, in order. The first one Revit
# accepts is used (e.g. a callout to a drafting view only accepts drafting
# types). Set to an empty list to keep whatever type Revit assigns.
CALLOUT_TYPE_NAMES = ["ViewAQC Callout", "ViewAQC"]
 
# View types that can host a reference callout (per ViewSection.CreateReferenceCallout).
PARENT_VIEW_TYPES = (
    ViewType.FloorPlan,
    ViewType.CeilingPlan,
    ViewType.EngineeringPlan,
    ViewType.Section,
    ViewType.Elevation,
    ViewType.DraftingView,
    ViewType.Detail,
)
 
 
class CalloutSelectionFilter(ISelectionFilter):
    """Only allow view markers (callouts, sections, etc.) to be picked."""
 
    def AllowElement(self, element):
        cat = element.Category
        return cat is not None and cat.Id == ElementId(BuiltInCategory.OST_Viewers)
 
    def AllowReference(self, reference, position):
        return False
 
 
def element_name(element):
    """Name of any element, including types. IronPython cannot read .Name
    directly on ElementType subclasses such as ViewFamilyType."""
    try:
        return Element.Name.GetValue(element)
    except Exception:
        param = element.get_Parameter(BuiltInParameter.SYMBOL_NAME_PARAM)
        return param.AsString() if param is not None else ""
 
 
def find_view_by_name(name):
    """Return the non-template view whose name matches exactly, or None."""
    for view in FilteredElementCollector(doc).OfClass(View):
        if not view.IsTemplate and view.Name == name:
            return view
    return None
 
 
def pick_callouts():
    """Every pre-selected callout marker, or whatever the user picks.
 
    Returns a list of marker elements (duplicates removed, order kept).
    """
    callout_cat_id = ElementId(BuiltInCategory.OST_Viewers)
    markers = []
    seen = set()
 
    def add(el):
        if el is None or el.Category is None:
            return
        if el.Category.Id != callout_cat_id:
            return
        if el.Id.Value in seen:
            return
        seen.add(el.Id.Value)
        markers.append(el)
 
    for el_id in uidoc.Selection.GetElementIds():
        add(doc.GetElement(el_id))
    if markers:
        return markers
 
    try:
        refs = uidoc.Selection.PickObjects(
            ObjectType.Element,
            CalloutSelectionFilter(),
            "Select the callouts to copy as reference callouts, then click Finish",
        )
    except OperationCanceledException:
        script.exit()
    for ref in refs:
        add(doc.GetElement(ref.ElementId))
    return markers
 
 
def build_callout_view_lookup(parent_view):
    """Map {view name: callout View} for every callout owned by parent_view.
 
    Built once so the whole selection costs a single pass over the views.
    """
    lookup = {}
    for view in FilteredElementCollector(doc).OfClass(View):
        try:
            if (
                not view.IsTemplate
                and view.IsCallout
                and view.GetCalloutParentId() == parent_view.Id
            ):
                lookup[view.Name] = view
        except Exception:
            continue
    return lookup
 
 
class CalloutError(Exception):
    """Raised for expected failures while building the callout."""
 
 
def box_corners(bbox):
    """All 8 corners of a BoundingBoxXYZ, in model coordinates."""
    t = bbox.Transform
    mn, mx = bbox.Min, bbox.Max
    corners = []
    for x in (mn.X, mx.X):
        for y in (mn.Y, mx.Y):
            for z in (mn.Z, mx.Z):
                corners.append(t.OfPoint(XYZ(x, y, z)))
    return corners
 
 
def project_to_view(point, view):
    """Project a model point onto the view's plane."""
    offset = point - view.Origin
    return (
        view.Origin
        + view.RightDirection.Multiply(offset.DotProduct(view.RightDirection))
        + view.UpDirection.Multiply(offset.DotProduct(view.UpDirection))
    )
 
 
def angle_in_view(direction, view):
    """Angle (radians) of a direction in the view plane, measured from the
    view's Right toward its Up (counter-clockwise on screen)."""
    return math.atan2(
        direction.DotProduct(view.UpDirection),
        direction.DotProduct(view.RightDirection),
    )
 
 
def normalize_quarter_turn(angle):
    """Fold an angle into (-45, 45] degrees. A rectangle's edges only give
    its rotation modulo 90 degrees."""
    quarter = math.pi / 2.0
    angle = math.fmod(angle, quarter)
    if angle > quarter / 2.0:
        angle -= quarter
    elif angle <= -quarter / 2.0:
        angle += quarter
    return angle
 
 
def describe_source(marker, parent_view, callout_views):
    """Collect what is needed to reproduce one selected callout.
 
    Returns a dict with:
      kind      - readable description of the callout
      type_id   - ElementId of the callout's type (ViewFamilyType)
      loop      - boundary CurveLoop in model coords, or None
      sketched  - True if the boundary is a non-rectangular sketch
      rotation  - rotation (radians) in the parent view plane
    """
    info = {
        "kind": None,
        "type_id": marker.GetTypeId(),
        "loop": None,
        "sketched": False,
        "rotation": 0.0,
    }
 
    # Reference callouts have no view of their own, so they are absent here.
    callout_view = callout_views.get(marker.Name)
    try:
        if callout_view is not None:
            manager = callout_view.GetCropRegionShapeManager()
            info["kind"] = "Callout view ({})".format(callout_view.ViewType)
            if info["type_id"] == ElementId.InvalidElementId:
                info["type_id"] = callout_view.GetTypeId()
            # The callout view's own axes give its true rotation.
            info["rotation"] = angle_in_view(callout_view.RightDirection, parent_view)
        else:
            manager = View.GetCropRegionShapeManagerForReferenceCallout(doc, marker.Id)
            info["kind"] = "Reference callout"
        info["sketched"] = manager.ShapeSet
        loops = manager.GetCropShape()
        if loops is not None and loops.Count > 0:
            info["loop"] = loops[0]
    except Exception:
        info["kind"] = info["kind"] or "Unknown callout"
        return info
 
    # A reference callout has no view of its own; for a rectangular one the
    # rotation can still be recovered from the direction of its edges.
    if callout_view is None and info["loop"] is not None and not info["sketched"]:
        for curve in info["loop"]:
            if isinstance(curve, Line):
                info["rotation"] = normalize_quarter_turn(
                    angle_in_view(curve.Direction, parent_view)
                )
                break
 
    type_el = doc.GetElement(info["type_id"])
    if type_el is not None:
        info["kind"] = "{} - {}".format(info["kind"], element_name(type_el))
    return info
 
 
def flatten_loop(loop, view, transform=None):
    """Rebuild a CurveLoop of straight lines projected onto the view's plane,
    optionally transformed. Returns None if the loop contains anything other
    than lines (Revit only allows straight segments in a callout sketch)."""
    tolerance = doc.Application.ShortCurveTolerance
    flat = CurveLoop()
    for curve in loop:
        if not isinstance(curve, Line):
            return None
        start = project_to_view(curve.GetEndPoint(0), view)
        end = project_to_view(curve.GetEndPoint(1), view)
        if transform is not None:
            start, end = transform.OfPoint(start), transform.OfPoint(end)
        if start.DistanceTo(end) > tolerance:
            flat.Append(Line.CreateBound(start, end))
    return flat
 
 
def extents_corners(points, view):
    """Two diagonal corners (in the view plane) enclosing all points."""
    origin, right, up = view.Origin, view.RightDirection, view.UpDirection
    us = [(p - origin).DotProduct(right) for p in points]
    vs = [(p - origin).DotProduct(up) for p in points]
    p1 = origin + right.Multiply(min(us)) + up.Multiply(min(vs))
    p2 = origin + right.Multiply(max(us)) + up.Multiply(max(vs))
    return p1, p2
 
 
def build_geometry(source, marker, parent_view):
    """Work out the placement for one new callout.
 
    Revit only accepts two axis-aligned corners, so the shape is un-rotated
    first, placed, and rotated back afterwards.
 
    Returns (p1, p2, pivot, rotation, unrotated_loop), or None if neither the
    boundary nor the marker's extents could be read.
    """
    rotation = source["rotation"]
    pivot = None
    unrotated_loop = None
 
    if source["loop"] is not None:
        flat = flatten_loop(source["loop"], parent_view)
        if flat is not None:
            pts = [c.GetEndPoint(0) for c in flat]
            if pts:
                pivot = XYZ(
                    sum(p.X for p in pts) / len(pts),
                    sum(p.Y for p in pts) / len(pts),
                    sum(p.Z for p in pts) / len(pts),
                )
                unrotate = Transform.CreateRotationAtPoint(
                    parent_view.ViewDirection, -rotation, pivot
                )
                unrotated_loop = flatten_loop(source["loop"], parent_view, unrotate)
 
    if unrotated_loop is not None:
        points = [c.GetEndPoint(0) for c in unrotated_loop]
    else:
        # No readable shape: fall back to the marker's rectangular extents.
        bbox = marker.get_BoundingBox(parent_view)
        if bbox is None:
            return None
        points = box_corners(bbox)
        pivot = None
        rotation = 0.0
 
    p1, p2 = extents_corners(points, parent_view)
    return p1, p2, pivot, rotation, unrotated_loop
 
 
def get_callout_types():
    """Return the ViewFamilyTypes named in CALLOUT_TYPE_NAMES, in that order.
    A name may match types in several view families; all are included."""
    all_types = list(FilteredElementCollector(doc).OfClass(ViewFamilyType))
    found = []
    for name in CALLOUT_TYPE_NAMES:
        found.extend(vft for vft in all_types if element_name(vft) == name)
    return found
 
 
def apply_callout_type(new_id, candidates):
    """Try each candidate type on the new callout until one holds.
    Returns (name of the type applied or None, list of failure messages)."""
    current = doc.GetElement(new_id).GetTypeId()
    if any(vft.Id == current for vft in candidates):
        return element_name(doc.GetElement(current)), []
 
    errors = []
    for vft in candidates:
        def apply_type():
            doc.GetElement(new_id).ChangeTypeId(vft.Id)
            # Revit may silently swap the type back on regeneration if it
            # is not valid for the referenced view, so confirm it held.
            doc.Regenerate()
            if doc.GetElement(new_id).GetTypeId() != vft.Id:
                raise CalloutError("Revit reverted the type.")
 
        err = try_step("Type '{}'".format(element_name(vft)), apply_type)
        if err is None:
            return element_name(vft), []
        errors.append(err)
    return None, errors
 
 
def viewer_ids():
    """Ids of every view marker instance (callouts, sections, ...) in the model."""
    return set(
        i.Value for i in FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_Viewers)
        .WhereElementIsNotElementType()
        .ToElementIds()
    )
 
 
def find_new_callout(parent_view, existing_refs, existing_viewers):
    """Return the ElementId of the reference callout just created, or None.
 
    GetReferenceCallouts() does not always report the new callout, so fall
    back to diffing all view markers in the model, preferring one owned by
    the parent view.
    """
    for i in parent_view.GetReferenceCallouts():
        if i.Value not in existing_refs:
            return i
    new_ids = [ElementId(v) for v in viewer_ids() - existing_viewers]
    for el_id in new_ids:
        if doc.GetElement(el_id).OwnerViewId == parent_view.Id:
            return el_id
    return new_ids[0] if new_ids else None
 
 
def try_step(name, action):
    """Run an optional step in a SubTransaction. Returns an error message on
    failure (after rolling the step back), or None on success."""
    sub = SubTransaction(doc)
    sub.Start()
    try:
        action()
        sub.Commit()
        return None
    except Exception as ex:
        sub.RollBack()
        return "{}: {}".format(name, getattr(ex, "Message", str(ex)))
 
 
def process_marker(marker, parent_view, target_view, callout_types, callout_views,
                   delete_original):
    """Create one reference callout from one selected marker.
 
    Never raises: a failure is reported in the returned dict so the rest of
    the selection still goes through. Each step runs in its own
    SubTransaction, so a failed callout leaves nothing behind.
    """
    result = {
        "name": marker.Name,
        "new_id": None,
        "kind": "",
        "sketched": False,
        "rotation": 0.0,
        "type_name": "",
        "deleted": False,
        "warnings": [],
        "error": None,
    }
 
    marker_id = marker.Id
    try:
        source = describe_source(marker, parent_view, callout_views)
    except Exception as ex:
        result["error"] = "Could not read the callout: {}".format(
            getattr(ex, "Message", str(ex))
        )
        return result
 
    result["kind"] = source["kind"]
    result["sketched"] = source["sketched"]
 
    geometry = build_geometry(source, marker, parent_view)
    if geometry is None:
        result["error"] = "Could not read the extents of the callout."
        return result
    p1, p2, pivot, rotation, unrotated_loop = geometry
    result["rotation"] = rotation
 
    # Snapshot before every creation: callouts made earlier in this batch are
    # already in the model and must count as existing.
    existing_refs = set(i.Value for i in parent_view.GetReferenceCallouts())
    existing_viewers = viewer_ids()
    created = {}
 
    def create():
        ViewSection.CreateReferenceCallout(doc, parent_view.Id, target_view.Id, p1, p2)
        doc.Regenerate()
        new_id = find_new_callout(parent_view, existing_refs, existing_viewers)
        if new_id is None:
            raise CalloutError("The new reference callout could not be found.")
        created["id"] = new_id
 
    err = try_step("Create", create)
    if err:
        result["error"] = err
        return result
    new_id = created["id"]
    result["new_id"] = new_id
 
    # Callout type: the first of CALLOUT_TYPE_NAMES that Revit accepts.
    if callout_types:
        applied, errors = apply_callout_type(new_id, callout_types)
        if applied is None:
            result["warnings"].append(
                "None of the callout types could be applied; Revit's "
                "default was kept."
            )
            result["warnings"].extend(errors)
 
    # Sketched shape, only if the original was drawn with Sketch (applied
    # un-rotated; the rotation step turns it).
    if source["sketched"] and unrotated_loop is not None:
        def apply_shape():
            manager = View.GetCropRegionShapeManagerForReferenceCallout(doc, new_id)
            if not (manager.CanHaveShape and manager.IsCropRegionShapeValid(unrotated_loop)):
                raise CalloutError("Revit rejected the sketched boundary.")
            manager.SetCropShape(unrotated_loop)
 
        err = try_step("Sketched shape", apply_shape)
        if err:
            result["warnings"].append(err + " The callout is rectangular.")
    elif source["sketched"]:
        result["warnings"].append(
            "Sketched shape could not be read. The callout is rectangular."
        )
 
    # Rotation about the shape's centre, normal to the parent view.
    if pivot is not None and abs(rotation) > 1e-6:
        axis = Line.CreateBound(pivot, pivot + parent_view.ViewDirection)
        err = try_step(
            "Rotation",
            lambda: ElementTransformUtils.RotateElement(doc, new_id, axis, rotation),
        )
        if err:
            result["warnings"].append(err)
 
    # Optionally delete the original callout (and its view, if any).
    if delete_original:
        err = try_step("Delete original", lambda: doc.Delete(marker_id))
        if err:
            result["warnings"].append(err)
        else:
            result["deleted"] = True
 
    result["type_name"] = element_name(
        doc.GetElement(doc.GetElement(new_id).GetTypeId())
    )
    return result
 
 
def report(results, target_view, delete_original):
    """Show one summary alert, with the per-callout detail behind 'Show'."""
    created = [r for r in results if r["new_id"] is not None]
    failed = [r for r in results if r["new_id"] is None]
    warned = [r for r in created if r["warnings"]]
 
    message = "Created {} of {} reference callout(s) to '{}'.".format(
        len(created), len(results), target_view.Name
    )
    if delete_original:
        message += "\nDeleted originals: {}.".format(
            len([r for r in results if r["deleted"]])
        )
    if failed:
        message += "\nFailed: {}.".format(len(failed))
    if warned:
        message += "\nWith issues: {}.".format(len(warned))
 
    lines = []
    for r in results:
        lines.append("'{}'".format(r["name"]))
        if r["new_id"] is None:
            lines.append("    FAILED: {}".format(r["error"]))
            lines.append("")
            continue
        lines.append("    Source: {}".format(r["kind"]))
        lines.append(
            "    Created by: {}   Type: {}   Rotation: {:.2f} deg   Original: {}".format(
                "Sketch" if r["sketched"] else "Rectangle",
                r["type_name"],
                math.degrees(r["rotation"]),
                "Deleted" if r["deleted"] else "Kept",
            )
        )
        for warning in r["warnings"]:
            lines.append("    Issue: {}".format(warning))
        lines.append("")
 
    forms.alert(
        message,
        title="Copy Callouts as Reference",
        expanded="\n".join(lines),
    )
 
 
def main():
    # 1. Validate the active (parent) view.
    parent_view = doc.ActiveView
    if parent_view.ViewType not in PARENT_VIEW_TYPES:
        forms.alert(
            "The active view ({}) cannot host reference callouts.\n"
            "Open a plan, section, elevation, drafting or detail view.".format(
                parent_view.ViewType
            ),
            exitscript=True,
        )
 
    # 2. Validate the hard-coded target view.
    target_view = find_view_by_name(TARGET_VIEW_NAME)
    if target_view is None:
        forms.alert(
            "Target view '{}' was not found in this model.\n"
            "Update TARGET_VIEW_NAME in the script.".format(TARGET_VIEW_NAME),
            exitscript=True,
        )
    if target_view.Id == parent_view.Id:
        forms.alert("The target view cannot be the active view.", exitscript=True)
    if target_view.ViewType != ViewType.DraftingView and not target_view.CropBoxActive:
        forms.alert(
            "Target view '{}' must be cropped (or be a drafting view) "
            "to be referenced.".format(TARGET_VIEW_NAME),
            exitscript=True,
        )
 
    callout_types = get_callout_types()
    if CALLOUT_TYPE_NAMES and not callout_types:
        forms.alert(
            "None of the view types {} were found in this model.\n"
            "Update CALLOUT_TYPE_NAMES in the script.".format(
                ", ".join("'{}'".format(n) for n in CALLOUT_TYPE_NAMES)
            ),
            exitscript=True,
        )
 
    # 3. Pick the callouts.
    markers = pick_callouts()
    if not markers:
        forms.alert("No callouts were selected.", exitscript=True)
 
    delete_original = forms.alert(
        "Delete the {} selected callout(s) after creating the reference "
        "callouts?".format(len(markers)),
        sub_msg=(
            "This applies to the whole selection. For a callout view, the "
            "view itself (and its placement on any sheet) will be deleted "
            "too. This can be undone with Ctrl+Z."
        ),
        title="Copy Callouts as Reference",
        options=["Keep originals", "Delete originals"],
    )
    if delete_original is None:
        script.exit()
    delete_original = delete_original == "Delete originals"
 
    # 4. One transaction for the whole batch, so Ctrl+Z undoes it all at once.
    callout_views = build_callout_view_lookup(parent_view)
    results = []
 
    t = Transaction(doc, "Copy Callouts as Reference")
    t.Start()
    try:
        for marker in markers:
            results.append(
                process_marker(
                    marker,
                    parent_view,
                    target_view,
                    callout_types,
                    callout_views,
                    delete_original,
                )
            )
 
        new_ids = [r["new_id"] for r in results if r["new_id"] is not None]
        if not new_ids:
            t.RollBack()
            forms.alert(
                "No reference callouts could be created:\n\n- {}".format(
                    "\n- ".join(r["error"] for r in results if r["error"])
                ),
                title="Copy Callouts as Reference",
                exitscript=True,
            )
        t.Commit()
    except (ArgumentException, CalloutError) as ex:
        t.RollBack()
        forms.alert(
            "Revit could not create the reference callouts:\n\n{}".format(
                getattr(ex, "Message", str(ex))
            ),
            exitscript=True,
        )
    except Exception:
        t.RollBack()
        raise
 
    # 5. Select the new callouts and report.
    uidoc.Selection.SetElementIds(List[ElementId](new_ids))
    report(results, target_view, delete_original)
 
 
try:
    main()
except SystemExit:
    raise
except Exception:
    forms.alert(
        "Copy Callouts as Reference failed.",
        title="Copy Callouts as Reference",
        expanded=traceback.format_exc(),
    )