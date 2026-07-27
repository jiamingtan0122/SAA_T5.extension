# -*- coding: utf-8 -*-
__title__ = "Wall Analysis Export"
__author__ = "JM"
__doc__ = """Export wall attachment analysis to CSV."""

from pyrevit import revit, forms, DB
from Autodesk.Revit.DB import *
import csv
import os

doc = __revit__.ActiveUIDocument.Document
view = doc.ActiveView

Z_TOLERANCE_FT = 0.05
XY_BUFFER_FT = 0.01

CANDIDATE_CATEGORIES = [
    BuiltInCategory.OST_Floors,
    BuiltInCategory.OST_StructuralFraming,
    BuiltInCategory.OST_Roofs,
]


# ==========================================================
# Helper Functions
# ==========================================================
def ft_to_mm(val):
    return round(
        UnitUtils.ConvertFromInternalUnits(
            val,
            UnitTypeId.Millimeters
        ),
        1
    )


def get_attach_flag(wall, bip):
    try:
        p = wall.get_Parameter(bip)
        if p and p.HasValue:
            return "Yes" if p.AsInteger() == 1 else "No"
    except:
        pass
    return "No"


def bbox_minmax(bbox):
    return (
        bbox.Min.X,
        bbox.Min.Y,
        bbox.Min.Z,
        bbox.Max.X,
        bbox.Max.Y,
        bbox.Max.Z
    )


def transform_bbox_minmax(bbox, transform):
    mn = bbox.Min
    mx = bbox.Max

    corners = [
        XYZ(mn.X, mn.Y, mn.Z),
        XYZ(mx.X, mn.Y, mn.Z),
        XYZ(mn.X, mx.Y, mn.Z),
        XYZ(mx.X, mx.Y, mn.Z),
        XYZ(mn.X, mn.Y, mx.Z),
        XYZ(mx.X, mn.Y, mx.Z),
        XYZ(mn.X, mx.Y, mx.Z),
        XYZ(mx.X, mx.Y, mx.Z)
    ]

    pts = [transform.OfPoint(c) for c in corners]

    xs = [p.X for p in pts]
    ys = [p.Y for p in pts]
    zs = [p.Z for p in pts]

    return (
        min(xs),
        min(ys),
        min(zs),
        max(xs),
        max(ys),
        max(zs)
    )


def xy_overlap(a, b, buffer):
    return not (
        a[3] + buffer < b[0]
        or b[3] + buffer < a[0]
        or a[4] + buffer < b[1]
        or b[4] + buffer < a[1]
    )


def gather_candidate_bboxes(rvt_doc):
    candidates = []

    # Host Model
    for cat in CANDIDATE_CATEGORIES:
        try:
            elems = (
                FilteredElementCollector(rvt_doc)
                .OfCategory(cat)
                .WhereElementIsNotElementType()
            )

            for el in elems:
                bb = el.get_BoundingBox(None)
                if bb:
                    candidates.append(bbox_minmax(bb))
        except:
            pass

    # Linked Models
    links = FilteredElementCollector(
        rvt_doc
    ).OfClass(RevitLinkInstance)

    for link in links:
        try:
            link_doc = link.GetLinkDocument()
            if not link_doc:
                continue

            transform = link.GetTotalTransform()

            for cat in CANDIDATE_CATEGORIES:
                elems = (
                    FilteredElementCollector(link_doc)
                    .OfCategory(cat)
                    .WhereElementIsNotElementType()
                )

                for el in elems:
                    bb = el.get_BoundingBox(None)
                    if bb:
                        candidates.append(
                            transform_bbox_minmax(
                                bb,
                                transform
                            )
                        )
        except:
            pass

    return candidates


# ==========================================================
# Collect Walls
# ==========================================================
walls = (
    FilteredElementCollector(doc, view.Id)
    .OfCategory(BuiltInCategory.OST_Walls)
    .WhereElementIsNotElementType()
)

walls = [w for w in walls if isinstance(w, Wall)]

if not walls:
    forms.alert("No walls found.")
    script.exit()

candidates = gather_candidate_bboxes(doc)

report_data = []

# ==========================================================
# Analyse Walls
# ==========================================================
for w in walls:

    wall_id = w.Id.IntegerValue

    try:
        wall_type = doc.GetElement(
            w.GetTypeId()
        ).Name
    except:
        wall_type = ""

    # -------------------------
    # Base Level
    # -------------------------
    base_level = ""
    p = w.get_Parameter(
        BuiltInParameter.WALL_BASE_CONSTRAINT
    )

    if p:
        lvl = doc.GetElement(p.AsElementId())
        if lvl:
            base_level = lvl.Name

    # -------------------------
    # Top Level
    # -------------------------
    top_level = ""
    p = w.get_Parameter(
        BuiltInParameter.WALL_HEIGHT_TYPE
    )

    if p:
        lvl = doc.GetElement(p.AsElementId())
        if lvl:
            top_level = lvl.Name

    # -------------------------
    # Offsets
    # -------------------------
    base_offset = 0
    p = w.get_Parameter(
        BuiltInParameter.WALL_BASE_OFFSET
    )
    if p:
        base_offset = ft_to_mm(p.AsDouble())

    top_offset = 0
    p = w.get_Parameter(
        BuiltInParameter.WALL_TOP_OFFSET
    )
    if p:
        top_offset = ft_to_mm(p.AsDouble())

    # -------------------------
    # Attachment
    # -------------------------
    top_attached = get_attach_flag(
        w,
        BuiltInParameter.WALL_TOP_IS_ATTACHED
    )

    base_attached = get_attach_flag(
        w,
        BuiltInParameter.WALL_BOTTOM_IS_ATTACHED
    )

    # -------------------------
    # Wall Bounding Box
    # -------------------------
    wall_top_mm = ""
    slab_above_mm = ""
    gap_mm = ""

    bb = w.get_BoundingBox(None)

    if bb:
        wall_bbox = bbox_minmax(bb)

        wall_top_z = wall_bbox[5]
        wall_top_mm = ft_to_mm(wall_top_z)

        nearest_gap = None
        nearest_slab = None

        for c in candidates:

            if not xy_overlap(
                wall_bbox,
                c,
                XY_BUFFER_FT
            ):
                continue

            gap = c[2] - wall_top_z

            if gap >= -Z_TOLERANCE_FT:

                if (
                    nearest_gap is None
                    or gap < nearest_gap
                ):
                    nearest_gap = gap
                    nearest_slab = c[2]

        if nearest_gap is not None:
            slab_above_mm = ft_to_mm(
                nearest_slab
            )

            gap_mm = ft_to_mm(
                nearest_gap
            )

    report_data.append([
        wall_id,
        wall_type,
        base_level,
        base_offset,
        top_level,
        top_offset,
        wall_top_mm,
        slab_above_mm,
        gap_mm,
        top_attached,
        base_attached
    ])

# ==========================================================
# Export CSV
# ==========================================================
filepath = forms.save_file(
    file_ext='csv',
    default_name='Wall_Analysis_Report.csv'
)

if filepath:

    with open(filepath, 'wb') as f:
        writer = csv.writer(f)

        writer.writerow([
            'Element ID',
            'Wall Type',
            'Base Level',
            'Base Offset (mm)',
            'Top Level',
            'Top Offset (mm)',
            'Wall Top Elevation (mm)',
            'Slab Above Elevation (mm)',
            'Gap To Slab Above (mm)',
            'Top Attached',
            'Base Attached'
        ])

        for row in report_data:
            writer.writerow(row)

    forms.alert(
        "Export completed.\n\n{}".format(
            filepath
        )
    )