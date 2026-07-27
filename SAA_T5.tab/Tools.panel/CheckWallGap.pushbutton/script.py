# -*- coding: utf-8 -*-
"""pyRevit command: report wall top offsets from slab undersides above."""

from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    Element,
    FilteredElementCollector,
    RevitLinkInstance,
    Transform,
    XYZ,
)
from Autodesk.Revit.UI import (
    TaskDialog,
    TaskDialogCommandLinkId,
    TaskDialogCommonButtons,
    TaskDialogIcon,
    TaskDialogResult,
)
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

from pyrevit import forms, revit, script


MM_PER_FOOT = 304.8
DEFAULT_THRESHOLD_MM = 10.0
XY_PADDING_FT = 0.25
OVERLAP_TOL_FT = 1.0 / MM_PER_FOOT
MAX_SLAB_ABOVE_WALL_TOP_FT = 1000.0 / MM_PER_FOOT
MAX_SLAB_BELOW_WALL_TOP_FT = 1000.0 / MM_PER_FOOT


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

SCOPE_PICK_WALL = "pick_wall"
SCOPE_ALL_WALLS = "all_walls"


def safe_text(value, fallback=""):
    if value is None:
        return fallback
    try:
        return unicode(value)
    except NameError:
        try:
            return str(value)
        except Exception:
            return fallback


def element_id_value(element_id):
    if hasattr(element_id, "Value"):
        return element_id.Value
    return element_id.IntegerValue


def element_name(element):
    try:
        return safe_text(Element.Name.GetValue(element), "")
    except Exception:
        try:
            return safe_text(element.Name, "")
        except Exception:
            return ""


def is_wall(element):
    try:
        return (
            element is not None
            and element.Category is not None
            and element.Category.Id.IntegerValue == int(BuiltInCategory.OST_Walls)
        )
    except Exception:
        return False


class WallSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return is_wall(element)

    def AllowReference(self, reference, point):
        return False


def get_level_name(element):
    try:
        level_id = element.LevelId
        if level_id and element_id_value(level_id) > 0:
            level = element.Document.GetElement(level_id)
            if level:
                return element_name(level)
    except Exception:
        pass

    try:
        param = element.get_Parameter(BuiltInParameter.WALL_BASE_CONSTRAINT)
        if param:
            return safe_text(param.AsValueString(), "")
    except Exception:
        pass

    return ""


def parameter_value_text(element, built_in_parameter):
    try:
        param = element.get_Parameter(built_in_parameter)
    except Exception:
        param = None

    if not param:
        return ""

    try:
        value = param.AsValueString()
        if value:
            return safe_text(value)
    except Exception:
        pass

    try:
        value = param.AsString()
        if value:
            return safe_text(value)
    except Exception:
        pass

    try:
        return safe_text(param.AsDouble())
    except Exception:
        return ""


def get_wall_top_constraint(wall):
    for built_in_parameter in (
        BuiltInParameter.WALL_HEIGHT_TYPE,
        BuiltInParameter.WALL_USER_HEIGHT_PARAM,
    ):
        text = parameter_value_text(wall, built_in_parameter)
        if text:
            return text
    return ""


def get_wall_top_offset(wall):
    return parameter_value_text(wall, BuiltInParameter.WALL_TOP_OFFSET)


def get_bbox(element):
    try:
        return element.get_BoundingBox(None)
    except Exception:
        return None


def transformed_corners(bbox, transform):
    min_pt = bbox.Min
    max_pt = bbox.Max
    points = []
    for x in (min_pt.X, max_pt.X):
        for y in (min_pt.Y, max_pt.Y):
            for z in (min_pt.Z, max_pt.Z):
                point = XYZ(x, y, z)
                if transform:
                    point = transform.OfPoint(point)
                points.append(point)
    return points


def bbox_info(element, transform=None):
    bbox = get_bbox(element)
    if bbox is None:
        return None

    points = transformed_corners(bbox, transform)
    if not points:
        return None

    return {
        "min_x": min(p.X for p in points),
        "max_x": max(p.X for p in points),
        "min_y": min(p.Y for p in points),
        "max_y": max(p.Y for p in points),
        "min_z": min(p.Z for p in points),
        "max_z": max(p.Z for p in points),
    }


def xy_overlaps(a, b, padding=0.0):
    return not (
        a["max_x"] + padding < b["min_x"]
        or a["min_x"] - padding > b["max_x"]
        or a["max_y"] + padding < b["min_y"]
        or a["min_y"] - padding > b["max_y"]
    )


def format_mm(feet_value):
    return "{0:.1f}".format(feet_value * MM_PER_FOOT)


def collect_host_walls(model_doc):
    return list(
        FilteredElementCollector(model_doc)
        .OfCategory(BuiltInCategory.OST_Walls)
        .WhereElementIsNotElementType()
        .ToElements()
    )


def prompt_for_scope():
    dialog = TaskDialog("Check Wall Gap")
    dialog.MainIcon = TaskDialogIcon.TaskDialogIconInformation
    dialog.MainInstruction = "Choose walls to check."
    dialog.MainContent = "Pick one host wall for a focused query, or scan all host walls."
    dialog.CommonButtons = TaskDialogCommonButtons.Cancel
    dialog.AddCommandLink(
        TaskDialogCommandLinkId.CommandLink1,
        "Pick one wall",
        "Query the selected wall top offset from the slab underside above.",
    )
    dialog.AddCommandLink(
        TaskDialogCommandLinkId.CommandLink2,
        "Check all host walls",
        "Scan all wall instances in the active host model.",
    )

    result = dialog.Show()
    if result == TaskDialogResult.CommandLink1:
        return SCOPE_PICK_WALL
    if result == TaskDialogResult.CommandLink2:
        return SCOPE_ALL_WALLS
    return None


def pick_host_wall():
    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element,
            WallSelectionFilter(),
            "Pick a host wall to query top offset from slab underside.",
        )
    except Exception:
        return None

    if reference is None:
        return None

    try:
        wall = doc.GetElement(reference.ElementId)
    except Exception:
        wall = None

    if not is_wall(wall):
        return None

    return wall


def get_walls_for_scope(scope):
    if scope == SCOPE_PICK_WALL:
        wall = pick_host_wall()
        if wall is None:
            return None, "picked wall"
        return [wall], "picked wall"

    return collect_host_walls(doc), "all host walls"


def collect_floors_from_doc(model_doc, source_name, transform=None):
    floors = []
    for floor in (
        FilteredElementCollector(model_doc)
        .OfCategory(BuiltInCategory.OST_Floors)
        .WhereElementIsNotElementType()
        .ToElements()
    ):
        info = bbox_info(floor, transform)
        if info is None:
            continue
        floors.append(
            {
                "element": floor,
                "source": source_name,
                "name": element_name(floor),
                "id": element_id_value(floor.Id),
                "bbox": info,
            }
        )
    return floors


def collect_all_candidate_floors(model_doc):
    floors = collect_floors_from_doc(model_doc, "Host model")
    skipped_links = []

    link_instances = list(
        FilteredElementCollector(model_doc)
        .OfClass(RevitLinkInstance)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    for link in link_instances:
        link_name = element_name(link) or "Revit Link {0}".format(element_id_value(link.Id))
        try:
            link_doc = link.GetLinkDocument()
        except Exception:
            link_doc = None

        if link_doc is None:
            skipped_links.append(link_name)
            continue

        try:
            transform = link.GetTotalTransform()
        except Exception:
            transform = Transform.Identity

        floors.extend(collect_floors_from_doc(link_doc, "Link: {0}".format(link_name), transform))

    return floors, skipped_links


def nearest_slab_at_wall_top(wall_info, floors, search_window_ft):
    top_z = wall_info["max_z"]
    max_candidate_bottom_z = top_z + search_window_ft
    min_candidate_top_z = top_z - search_window_ft
    nearest = None
    nearest_offset = None

    for floor in floors:
        floor_info = floor["bbox"]
        if not xy_overlaps(wall_info, floor_info, XY_PADDING_FT):
            continue

        if floor_info["min_z"] > max_candidate_bottom_z:
            continue

        # Only inspect the slab nearest the wall top level. This avoids
        # reporting intermediate slabs crossed by tall multi-level walls.
        if floor_info["max_z"] < min_candidate_top_z:
            continue

        offset = floor_info["min_z"] - top_z

        if nearest_offset is None or abs(offset) < abs(nearest_offset):
            nearest = floor
            nearest_offset = offset

    return nearest, nearest_offset


def offset_status(offset_ft, threshold_ft):
    if abs(offset_ft) <= threshold_ft:
        return "Aligned"
    if offset_ft > 0:
        return "Wall top below slab underside"
    return "Wall top overlaps slab"


def prompt_threshold_mm():
    value = forms.ask_for_string(
        default=str(int(DEFAULT_THRESHOLD_MM)),
        prompt="Report wall top offsets from slab underside whose absolute value is greater than this tolerance (mm).",
        title="Check Wall Gap",
    )
    if value is None:
        return None

    try:
        threshold = float(value)
    except Exception:
        TaskDialog.Show("Check Wall Gap", "Invalid threshold: {0}".format(value))
        return None

    if threshold < 0:
        TaskDialog.Show("Check Wall Gap", "Threshold must be 0 or greater.")
        return None

    return threshold


def show_info(instruction, content=None):
    dialog = TaskDialog("Check Wall Gap")
    dialog.MainIcon = TaskDialogIcon.TaskDialogIconInformation
    dialog.MainInstruction = instruction
    if content:
        dialog.MainContent = content
    dialog.Show()


def main():
    scope = prompt_for_scope()
    if scope is None:
        return

    threshold_mm = prompt_threshold_mm()
    if threshold_mm is None:
        return

    walls, wall_source = get_walls_for_scope(scope)
    if walls is None:
        return

    floors, skipped_links = collect_all_candidate_floors(doc)

    if not walls:
        show_info("No host walls found in the active model.")
        return

    if not floors:
        show_info("No host or loaded linked floors found to check above the walls.")
        return

    threshold_ft = threshold_mm / MM_PER_FOOT
    # A tolerance larger than the old fixed 1000 mm window could never report
    # a positive gap beyond that window. Keep the normal 1000 mm locality, but
    # expand it to cover the tolerance selected by the user.
    search_window_ft = max(
        MAX_SLAB_ABOVE_WALL_TOP_FT,
        MAX_SLAB_BELOW_WALL_TOP_FT,
        threshold_ft,
    )
    rows = []
    issue_count = 0
    no_floor_above = []
    bbox_skipped = []

    for wall in walls:
        wall_info = bbox_info(wall)
        if wall_info is None:
            bbox_skipped.append(wall)
            continue

        floor, offset_ft = nearest_slab_at_wall_top(
            wall_info, floors, search_window_ft
        )
        if floor is None:
            no_floor_above.append(wall)
            continue

        exceeds_threshold = abs(offset_ft) > threshold_ft
        if exceeds_threshold:
            issue_count += 1

        # Always show a successfully matched wall/slab pair. Previously a match
        # inside the tolerance was omitted, which made a valid detection look
        # like the script had failed to find the slab.
        rows.append(
            {
                "wall": wall,
                "wall_info": wall_info,
                "floor": floor,
                "offset_ft": offset_ft,
                "exceeds_threshold": exceeds_threshold,
            }
        )

    output.print_md("# Check Wall Gap")
    output.print_md(
        "Checked **{0}** wall top(s) from **{1}** against **{2}** host/linked slab underside(s). Tolerance: **{3:.1f} mm**. Slab search window: **{4:.1f} mm below to {4:.1f} mm above wall top**.".format(
            len(walls), wall_source, len(floors), threshold_mm,
            search_window_ft * MM_PER_FOOT
        )
    )

    if skipped_links:
        output.print_md("\n**Skipped unloaded links:** {0}".format(", ".join(skipped_links)))

    output.print_md(
        "\n## Matched Wall Tops ({0}); Outside Tolerance ({1})".format(
            len(rows), issue_count
        )
    )

    if rows:
        rows.sort(key=lambda item: abs(item["offset_ft"]), reverse=True)
        table_rows = []
        for item in rows:
            wall = item["wall"]
            floor = item["floor"]
            wall_top_z = item["wall_info"]["max_z"]
            slab_bottom_z = floor["bbox"]["min_z"]
            table_rows.append(
                [
                    output.linkify(wall.Id),
                    element_name(wall),
                    get_level_name(wall),
                    get_wall_top_constraint(wall),
                    get_wall_top_offset(wall),
                    format_mm(wall_top_z),
                    format_mm(slab_bottom_z),
                    format_mm(item["offset_ft"]),
                    offset_status(item["offset_ft"], threshold_ft),
                    "Yes" if item["exceeds_threshold"] else "No",
                    floor["source"],
                    floor["name"],
                    str(floor["id"]),
                ]
            )

        output.print_table(
            table_data=table_rows,
            columns=[
                "Wall Id",
                "Wall Name",
                "Wall Level",
                "Top Constraint/Height",
                "Top Offset Param",
                "Wall Top Elev mm",
                "Slab Bottom Elev mm",
                "Signed Offset mm",
                "Status",
                "Outside Tolerance",
                "Slab Source",
                "Slab Name",
                "Slab Id",
            ],
        )
    else:
        output.print_md("No wall/slab matches were found in the search window.")

    output.print_md("\n## Not Checked")
    output.print_md("Walls with no overlapping slab candidate: **{0}**".format(len(no_floor_above)))
    output.print_md("Walls skipped because bounding box was unavailable: **{0}**".format(len(bbox_skipped)))

    if no_floor_above:
        sample = no_floor_above[:50]
        output.print_md("\n### Sample Walls With No Slab Candidate")
        output.print_table(
            table_data=[[output.linkify(w.Id), element_name(w), get_level_name(w)] for w in sample],
            columns=["Wall Id", "Wall Name", "Wall Level"],
        )
        if len(no_floor_above) > len(sample):
            output.print_md("Showing first {0} of {1}.".format(len(sample), len(no_floor_above)))

    show_info(
        "Wall gap check complete.",
        "Matched {0} wall(s) to a slab underside; {1} are outside the {2:.1f} mm tolerance. See pyRevit output for details.".format(
            len(rows), issue_count, threshold_mm
        ),
    )


if __name__ == "__main__":
    main()
