# -*- coding: utf-8 -*-
"""
Zoom to Selection
Version: 1.0
Date: 2026-09-01
Description: Zooms and centers the active view on the bounding box of the
             current selection. Revit has no native "Zoom to Selection"
             command, so this computes the selection's bounding box and
             calls UIView.ZoomAndCenterRectangle() on the active view.
"""

__author__ = "JM"

import traceback
from Autodesk.Revit.DB import XYZ, BoundingBoxXYZ
from pyrevit import revit, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()


def safe_str(s):
    try:
        return unicode(s)
    except:
        return str(s)


def get_selection_bbox():
    sel_ids = uidoc.Selection.GetElementIds()
    if not sel_ids:
        return None

    bbox = None
    for eid in sel_ids:
        el = doc.GetElement(eid)
        if not el:
            continue
        el_bbox = el.get_BoundingBox(doc.ActiveView)
        if el_bbox is None:
            el_bbox = el.get_BoundingBox(None)
        if el_bbox is None:
            continue

        if bbox is None:
            bbox = BoundingBoxXYZ()
            bbox.Min = el_bbox.Min
            bbox.Max = el_bbox.Max
        else:
            bbox.Min = XYZ(
                min(bbox.Min.X, el_bbox.Min.X),
                min(bbox.Min.Y, el_bbox.Min.Y),
                min(bbox.Min.Z, el_bbox.Min.Z),
            )
            bbox.Max = XYZ(
                max(bbox.Max.X, el_bbox.Max.X),
                max(bbox.Max.Y, el_bbox.Max.Y),
                max(bbox.Max.Z, el_bbox.Max.Z),
            )
    return bbox


def zoom_to_selection():
    bbox = get_selection_bbox()
    if bbox is None:
        script.get_logger().warning("Nothing selected, or selected elements have no visible geometry in this view.")
        return

    # pad by 20% so elements aren't flush against the view edge
    pad_x = (bbox.Max.X - bbox.Min.X) * 0.2 or 1.0
    pad_y = (bbox.Max.Y - bbox.Min.Y) * 0.2 or 1.0

    pt1 = XYZ(bbox.Min.X - pad_x, bbox.Min.Y - pad_y, 0)
    pt2 = XYZ(bbox.Max.X + pad_x, bbox.Max.Y + pad_y, 0)

    active_ui_view = None
    for uiview in uidoc.GetOpenUIViews():
        if uiview.ViewId == doc.ActiveView.Id:
            active_ui_view = uiview
            break

    if active_ui_view is None:
        script.get_logger().error("Could not find an open UIView for the active view.")
        return

    active_ui_view.ZoomAndCenterRectangle(pt1, pt2)


if __name__ == "__main__":
    try:
        zoom_to_selection()
    except Exception:
        print(safe_str(traceback.format_exc()))