# -*- coding: utf-8 -*-
__title__ = "Fix Area\nBoundary"
__author__ = "JM"
__doc__ = """Version = 1.2
Date    = 13.04.2025
_____________________________________________________________________
Description:

Select the area boundary lines forming a loop FIRST, then run.

Analyses the selected area separation lines only and:

  1. FLATTEN Z — projects all selected boundary lines to the exact
     elevation of the active view's level. Lines at different Z
     heights silently prevent areas from bounding — this fixes it.

  2. DIAGNOSES gaps — finds endpoints that do not connect to any
     other line within tolerance and reports location + gap size.

  3. HEALS small gaps — if two endpoints are within the auto-heal
     tolerance (default 5mm), extends one line to meet the other.

  4. REMOVES duplicates — deletes boundary lines whose geometry
     fully overlaps another line at the same location.

  5. REMOVES zero-length lines — deletes degenerate segments
     shorter than 1mm.

IMPORTANT: Pre-select the boundary lines for a specific loop
before running. The script only works on selected elements.
_____________________________________________________________________
"""

from pyrevit import revit, DB, script
from pyrevit.forms import alert
from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    Line,
    Transaction,
    FailureSeverity,
    FailureProcessingResult,
    XYZ,
)
import sys
import math

output = script.get_output()

# -----------------------------------------------------------------------
# Config (Revit internal units = feet)
# -----------------------------------------------------------------------
GAP_REPORT_TOLERANCE  = 0.5  / 304.8   # 0.5 mm
GAP_HEAL_TOLERANCE    = 5.0  / 304.8   # 5 mm  — auto-heal up to this
ZERO_LENGTH_THRESHOLD = 1.0  / 304.8   # 1 mm  — shorter = zero-length
Z_MISMATCH_THRESHOLD  = 0.1  / 304.8   # 0.1 mm — report Z diff above this


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
# Helpers
# -----------------------------------------------------------------------
def dist2d(a, b):
    return math.sqrt((a.X - b.X) ** 2 + (a.Y - b.Y) ** 2)


def pt_key(pt, decimals=4):
    return (round(pt.X, decimals), round(pt.Y, decimals))


def get_endpoints(elem):
    try:
        c = elem.Location.Curve
        if c is None:
            return None, None
        return c.GetEndPoint(0), c.GetEndPoint(1)
    except:
        return None, None


def get_curve(elem):
    try:
        return elem.Location.Curve
    except:
        return None


# -----------------------------------------------------------------------
# Get view level elevation
# -----------------------------------------------------------------------
def get_view_level_elevation(view, rvt_doc):
    """
    Returns the elevation (Z in feet) of the level associated with
    the active view. Falls back to view's origin Z if no level found.
    """
    try:
        level_id = view.LevelId
        if level_id and level_id != DB.ElementId.InvalidElementId:
            level = rvt_doc.GetElement(level_id)
            if level:
                return level.Elevation
    except:
        pass
    # Fallback: try GenLevel parameter
    try:
        p = view.get_Parameter(BuiltInParameter.PLAN_VIEW_LEVEL)
        if p:
            level = rvt_doc.GetElement(p.AsElementId())
            if level:
                return level.Elevation
    except:
        pass
    # Last resort: view origin Z
    try:
        return view.Origin.Z
    except:
        return 0.0


# -----------------------------------------------------------------------
# Filter selection to boundary lines only
# -----------------------------------------------------------------------
def filter_boundary_lines(rvt_doc, sel_ids):
    target_cat = int(BuiltInCategory.OST_AreaSchemeLines)
    lines = []
    for eid in sel_ids:
        elem = rvt_doc.GetElement(eid)
        if elem is None:
            continue
        cat = elem.Category
        if cat and cat.Id.IntegerValue == target_cat:
            lines.append(elem)
    return lines


# -----------------------------------------------------------------------
# Step 1 — Flatten Z
# -----------------------------------------------------------------------
def flatten_z(elems, target_z):
    """
    For each element, if either endpoint has a Z != target_z,
    replace the curve with one at target_z.
    Returns (flattened_count, already_flat_count, error_count,
             mismatch_report) where mismatch_report is a list of
             (elem_id, z_start, z_end).
    """
    flattened     = 0
    already_flat  = 0
    errors        = 0
    mismatch_info = []

    for elem in elems:
        c = get_curve(elem)
        if c is None:
            errors += 1
            continue

        s   = c.GetEndPoint(0)
        end = c.GetEndPoint(1)

        z_diff_s   = abs(s.Z   - target_z)
        z_diff_e   = abs(end.Z - target_z)

        if z_diff_s < Z_MISMATCH_THRESHOLD and z_diff_e < Z_MISMATCH_THRESHOLD:
            already_flat += 1
            continue

        # Record mismatch before fixing
        mismatch_info.append((elem.Id.IntegerValue, s.Z, end.Z))

        # Project both endpoints to target_z
        new_s   = XYZ(s.X,   s.Y,   target_z)
        new_end = XYZ(end.X, end.Y, target_z)

        if dist2d(new_s, new_end) < ZERO_LENGTH_THRESHOLD:
            errors += 1
            continue

        try:
            if isinstance(c, Line):
                elem.Location.Curve = Line.CreateBound(new_s, new_end)
                flattened += 1
            else:
                # Non-linear curve — skip (arcs etc.)
                errors += 1
        except:
            errors += 1

    return flattened, already_flat, errors, mismatch_info


# -----------------------------------------------------------------------
# Steps 2-5 — Gap / duplicate / zero-length analysis
# -----------------------------------------------------------------------
def find_zero_length(elems):
    ids = []
    for e in elems:
        c = get_curve(e)
        if c is None:
            continue
        try:
            if c.Length < ZERO_LENGTH_THRESHOLD:
                ids.append(e.Id)
        except:
            pass
    return ids


def find_duplicates(elems):
    seen      = {}
    to_delete = []
    for e in elems:
        s, end = get_endpoints(e)
        if s is None:
            continue
        ks  = pt_key(s)
        ke  = pt_key(end)
        key = (min(ks, ke), max(ks, ke))
        if key in seen:
            to_delete.append(e.Id)
        else:
            seen[key] = e
    return to_delete


def build_endpoint_list(elems):
    pts = []
    for e in elems:
        s, end = get_endpoints(e)
        if s is None:
            continue
        pts.append((s,   e, 0))
        pts.append((end, e, 1))
    return pts


def find_gaps(endpoint_list):
    all_gaps  = []
    healable  = []
    open_ends = []

    for i, (pt_a, elem_a, idx_a) in enumerate(endpoint_list):
        nearest_dist = float('inf')
        nearest_pt   = None

        for j, (pt_b, elem_b, idx_b) in enumerate(endpoint_list):
            if i == j or elem_a.Id == elem_b.Id:
                continue
            d = dist2d(pt_a, pt_b)
            if d < nearest_dist:
                nearest_dist = d
                nearest_pt   = pt_b

        if nearest_dist > GAP_REPORT_TOLERANCE:
            entry = (pt_a, elem_a, idx_a, nearest_dist, nearest_pt)
            all_gaps.append(entry)
            if nearest_dist <= GAP_HEAL_TOLERANCE:
                healable.append(entry)
            else:
                open_ends.append(entry)

    return all_gaps, healable, open_ends


def heal_gap(elem, idx, target_pt):
    try:
        c = elem.Location.Curve
        if not isinstance(c, Line):
            return False
        s   = c.GetEndPoint(0)
        end = c.GetEndPoint(1)
        new_s   = target_pt if idx == 0 else s
        new_end = target_pt if idx == 1 else end
        if dist2d(new_s, new_end) < ZERO_LENGTH_THRESHOLD:
            return False
        elem.Location.Curve = Line.CreateBound(new_s, new_end)
        return True
    except:
        return False


# -----------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------
doc   = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument
view  = doc.ActiveView

# ---- Guard: require pre-selection ------------------------------------
sel_ids = list(uidoc.Selection.GetElementIds())

if not sel_ids:
    alert(
        "Nothing selected.\n\n"
        "Select the area boundary lines for a specific loop first, "
        "then run this tool.",
        title="Fix Area Boundary — No Selection"
    )
    sys.exit()

boundary_lines = filter_boundary_lines(doc, sel_ids)

if not boundary_lines:
    alert(
        "No area boundary lines found in the selection.\n\n"
        "Select area separation lines (the magenta boundary lines "
        "in an Area Plan view), then run again.",
        title="Fix Area Boundary — No Boundary Lines"
    )
    sys.exit()

# ---- Get target Z from view level ------------------------------------
target_z = get_view_level_elevation(view, doc)

output.print_md("# Fix Area Boundary")
output.print_md("**Selected lines:** {}  |  "
                "**View level Z:** {:.4f}m".format(
                    len(boundary_lines),
                    target_z * 0.3048))
output.print_md("---")

# ---- Pre-scan: check which lines have Z mismatches ------------------
z_mismatches = []
for e in boundary_lines:
    c = get_curve(e)
    if c is None:
        continue
    s   = c.GetEndPoint(0)
    end = c.GetEndPoint(1)
    if (abs(s.Z - target_z) > Z_MISMATCH_THRESHOLD or
            abs(end.Z - target_z) > Z_MISMATCH_THRESHOLD):
        z_mismatches.append((e.Id.IntegerValue, s.Z, end.Z))

# ---- Pre-scan: zero / duplicates / gaps (for reporting) -------------
zero_ids_pre   = find_zero_length(boundary_lines)
zero_id_set    = set(i.IntegerValue for i in zero_ids_pre)
non_zero       = [e for e in boundary_lines
                  if e.Id.IntegerValue not in zero_id_set]
dup_ids_pre    = find_duplicates(non_zero)
dup_id_set     = set(i.IntegerValue for i in dup_ids_pre)
clean          = [e for e in non_zero
                  if e.Id.IntegerValue not in dup_id_set]
ep_list        = build_endpoint_list(clean)
_, healable, open_ends = find_gaps(ep_list)

# ---- Report ---------------------------------------------------------
output.print_md("## Analysis")
output.print_md("| Check | Count |")
output.print_md("|---|---|")
output.print_md("| Lines with wrong Z elevation | **{}** |".format(
    len(z_mismatches)))
output.print_md("| Zero-length lines | **{}** |".format(len(zero_ids_pre)))
output.print_md("| Duplicate lines | **{}** |".format(len(dup_ids_pre)))
output.print_md("| Auto-healable gaps (≤ {:.0f}mm) | **{}** |".format(
    GAP_HEAL_TOLERANCE * 304.8, len(healable)))
output.print_md("| Open gaps — manual fix needed (> {:.0f}mm) | **{}** |".format(
    GAP_HEAL_TOLERANCE * 304.8, len(open_ends)))

if z_mismatches:
    output.print_md("### Z elevation mismatches")
    output.print_md("| Element ID | Z start (m) | Z end (m) | Diff from level (mm) |")
    output.print_md("|---|---|---|---|")
    for eid, zs, ze in z_mismatches:
        diff = max(abs(zs - target_z), abs(ze - target_z)) * 304.8
        output.print_md("| {} | {:.4f} | {:.4f} | {:.2f}mm |".format(
            eid,
            zs * 0.3048,
            ze * 0.3048,
            diff
        ))

if open_ends:
    output.print_md("### Open gaps — manual fix required")
    output.print_md("| # | Gap (mm) | X (m) | Y (m) |")
    output.print_md("|---|---|---|---|")
    for i, (pt, elem, idx, d, nearest) in enumerate(open_ends, 1):
        output.print_md("| {} | {:.2f} | {:.4f} | {:.4f} |".format(
            i, d * 304.8, pt.X * 0.3048, pt.Y * 0.3048))
    output.print_md(
        "\n> Coordinates in metres from Revit model origin."
    )

if not z_mismatches and not zero_ids_pre and not dup_ids_pre and not healable:
    output.print_md(
        "\n**No automatic fixes available.** "
        "Fix open gaps manually and run again."
    )
    sys.exit()

# ---- Confirm --------------------------------------------------------
summary_lines = []
if z_mismatches:
    summary_lines.append(
        "Flatten {} line(s) to view level Z ({:.4f}m)".format(
            len(z_mismatches), target_z * 0.3048))
if zero_ids_pre:
    summary_lines.append(
        "Delete {} zero-length line(s)".format(len(zero_ids_pre)))
if dup_ids_pre:
    summary_lines.append(
        "Delete {} duplicate line(s)".format(len(dup_ids_pre)))
if healable:
    summary_lines.append(
        "Heal {} gap(s) up to {:.0f}mm".format(
            len(healable), GAP_HEAL_TOLERANCE * 304.8))

confirmed = alert(
    "Automatic fixes to apply:\n\n"
    + "\n".join("  \u2022 " + s for s in summary_lines)
    + "\n\nUndoable with Ctrl+Z. Proceed?",
    title="Fix Area Boundary \u2014 Confirm",
    yes=True, no=True
)
if not confirmed:
    output.print_md("**Cancelled.**")
    sys.exit()

# ---- Apply in one transaction ---------------------------------------
flattened = 0
deleted   = 0
healed    = 0
errors    = 0

t = Transaction(doc, "Fix Area Boundary")
t.Start()

fh = t.GetFailureHandlingOptions()
fh.SetFailuresPreprocessor(AutoOKWarnings())
t.SetFailureHandlingOptions(fh)

try:
    # 1 — Flatten Z first (before gap analysis runs on corrected geometry)
    flat, _, flat_err, _ = flatten_z(boundary_lines, target_z)
    flattened = flat
    errors   += flat_err

    # 2 — Re-collect after flatten for accurate gap/duplicate checks
    zero_ids    = find_zero_length(boundary_lines)
    zero_id_set = set(i.IntegerValue for i in zero_ids)
    non_zero    = [e for e in boundary_lines
                   if e.Id.IntegerValue not in zero_id_set]
    dup_ids     = find_duplicates(non_zero)
    dup_id_set  = set(i.IntegerValue for i in dup_ids)
    clean       = [e for e in non_zero
                   if e.Id.IntegerValue not in dup_id_set]

    # 3 — Delete zero-length
    for eid in zero_ids:
        try:
            doc.Delete(eid)
            deleted += 1
        except:
            errors += 1

    # 4 — Delete duplicates
    for eid in dup_ids:
        try:
            doc.Delete(eid)
            deleted += 1
        except:
            errors += 1

    # 5 — Heal small gaps (on post-flatten geometry)
    ep_list_post        = build_endpoint_list(clean)
    _, healable_post, _ = find_gaps(ep_list_post)
    healed_ids          = set()
    for pt, elem, idx, d, nearest_pt in healable_post:
        if elem.Id.IntegerValue in healed_ids:
            continue
        if nearest_pt is None:
            continue
        if heal_gap(elem, idx, nearest_pt):
            healed += 1
            healed_ids.add(elem.Id.IntegerValue)
        else:
            errors += 1

    t.Commit()

except Exception as ex:
    if t.HasStarted():
        t.RollBack()
    alert(
        "Transaction failed:\n{}".format(ex),
        title="Fix Area Boundary \u2014 Error"
    )
    sys.exit()

# ---- Final report ---------------------------------------------------
output.print_md("## Result")
output.print_md("| Action | Count |")
output.print_md("|---|---|")
output.print_md("| Lines flattened to view level | **{}** |".format(flattened))
output.print_md("| Lines deleted | **{}** |".format(deleted))
output.print_md("| Gaps healed | **{}** |".format(healed))
if errors:
    output.print_md("| Errors | **{}** |".format(errors))

if open_ends:
    output.print_md(
        "\n**{} open gap(s) still need manual repair.** "
        "See coordinates above.".format(len(open_ends))
    )
else:
    output.print_md(
        "\n**All issues resolved.** "
        "Try computing the area — the boundary should now close."
    )