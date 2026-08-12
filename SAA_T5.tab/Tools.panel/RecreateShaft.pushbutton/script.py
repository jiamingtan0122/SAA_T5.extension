# -*- coding: utf-8 -*-
"""pyRevit command: recreate all Shaft Openings from a chosen link.

Version: 2.0
Date: 2026-08-04
Description:
    Shows a picker of currently loaded Revit links in the model. Once a link
    is chosen, every Shaft Opening found in that linked document is read
    (sketch boundary, base/top level constraints and offsets), transformed
    into host coordinates using the link's placement transform, and recreated
    as a native Shaft Opening in the current (host) model so it will actually
    cut host floors/roofs.
"""

__author__ = "JM"

from collections import Counter
import traceback

from Autodesk.Revit.DB import (
    BuiltInCategory,
    BuiltInParameter,
    CheckoutStatus,
    CurveArray,
    Element,
    ElementId,
    FailureProcessingResult,
    FailureSeverity,
    FilteredElementCollector,
    FilteredWorksetCollector,
    IFailuresPreprocessor,
    Level,
    Line,
    RevitLinkInstance,
    Transaction,
    TransactionGroup,
    TransactionStatus,
    Workset,
    WorksetDefaultVisibilitySettings,
    WorksetKind,
    WorksharingUtils,
    XYZ,
)
from System.Collections.Generic import List
from Autodesk.Revit.UI import TaskDialog, TaskDialogIcon

from pyrevit import forms, revit, script


doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

SHAFT_BIC = BuiltInCategory.OST_ShaftOpening
COPIED_SHAFT_WORKSET_NAME = "ARCH_SAA_CORE SHAFT"
MM_PER_FOOT = 304.8

# Revit UI parameter names used on Shaft Opening instances. These match the
# English-locale Properties palette; adjust if the project is in another
# language.
PARAM_BASE_CONSTRAINT = "Base Constraint"
PARAM_BASE_OFFSET = "Base Offset"
PARAM_TOP_CONSTRAINT = "Top Constraint"
PARAM_TOP_OFFSET = "Top Offset"
PARAM_UNCONNECTED_HEIGHT = "Unconnected Height"


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


def is_shaft_opening(element):
    try:
        return (
            element is not None
            and element.Category is not None
            and element.Category.Id.IntegerValue == int(SHAFT_BIC)
        )
    except Exception:
        return False


def get_level_param_id(element, param_name):
    param = element.LookupParameter(param_name)
    if param is None:
        return None
    try:
        return param.AsElementId()
    except Exception:
        return None


def get_double_param(element, param_name):
    param = element.LookupParameter(param_name)
    if param is None:
        return None
    try:
        return param.AsDouble()
    except Exception:
        return None


def set_double_param(element, param_name, value):
    if value is None:
        return False
    param = element.LookupParameter(param_name)
    if param is None or param.IsReadOnly:
        return False
    try:
        param.Set(value)
        return True
    except Exception:
        return False


def set_level_param_id(element, param_name, level_id):
    param = element.LookupParameter(param_name)
    if param is None or param.IsReadOnly:
        return False
    try:
        param.Set(level_id)
        return True
    except Exception:
        return False


def collect_levels_by_name(model_doc):
    levels = {}
    for level in (
        FilteredElementCollector(model_doc).OfClass(Level).ToElements()
    ):
        levels[element_name(level)] = level
    return levels


def get_link_instances(host_doc):
    return list(
        FilteredElementCollector(host_doc)
        .OfClass(RevitLinkInstance)
        .WhereElementIsNotElementType()
        .ToElements()
    )


def collect_shaft_openings(link_doc):
    return list(
        FilteredElementCollector(link_doc)
        .OfCategory(SHAFT_BIC)
        .WhereElementIsNotElementType()
        .ToElements()
    )


def get_or_create_workset(host_doc, name):
    """Returns (workset, was_created). If a user workset with this name
    already exists, reuses it untouched. Otherwise creates it and turns off
    its "Visible in All Views" default so it doesn't show up everywhere."""
    for existing in FilteredWorksetCollector(host_doc).OfKind(WorksetKind.UserWorkset):
        if existing.Name == name:
            return existing, False

    new_workset = Workset.Create(host_doc, name)

    try:
        visibility_settings = WorksetDefaultVisibilitySettings.GetWorksetDefaultVisibilitySettings(host_doc)
        visibility_settings.SetWorksetVisibility(new_workset.Id, False)
    except Exception:
        pass

    return new_workset, True


def collect_host_shafts_in_workset(host_doc, workset_id):
    result = []
    for element in (
        FilteredElementCollector(host_doc)
        .OfCategory(SHAFT_BIC)
        .WhereElementIsNotElementType()
        .ToElements()
    ):
        try:
            if element.WorksetId == workset_id:
                result.append(element.Id)
        except Exception:
            pass
    return result


def get_other_user_owner(host_doc, element_id):
    """Returns the username currently holding this element (if it's
    checked out by someone other than us), or None if it's free to edit."""
    try:
        status = WorksharingUtils.GetCheckoutStatus(host_doc, element_id)
    except Exception:
        return None
    if status != CheckoutStatus.OwnedByOtherUser:
        return None
    try:
        return WorksharingUtils.GetWorksharingTooltipInfo(host_doc, element_id).Owner
    except Exception:
        return "another user"


class ShaftFailuresPreprocessor(IFailuresPreprocessor):
    """Suppress warnings and roll back a rejected shaft without a dialog.

    Revit only processes sketch failures when a real Transaction commits;
    a SubTransaction does not provide a failure-processing boundary.
    """

    def __init__(self):
        self.error_messages = []

    def PreprocessFailures(self, failures_accessor):
        failures = list(failures_accessor.GetFailureMessages())
        if not failures:
            return FailureProcessingResult.Continue

        for failure in failures:
            if failure.GetSeverity() == FailureSeverity.Warning:
                failures_accessor.DeleteWarning(failure)
            else:
                try:
                    message = safe_str(failure.GetDescriptionText())
                except Exception:
                    message = "Revit rejected the shaft opening."
                if message and message not in self.error_messages:
                    self.error_messages.append(message)

        if self.error_messages:
            return FailureProcessingResult.ProceedWithRollBack
        return FailureProcessingResult.ProceedWithCommit


def set_failure_handling(transaction, preprocessor):
    """Attaches quiet warning/error handling to one real Transaction."""
    options = transaction.GetFailureHandlingOptions()
    options.SetFailuresPreprocessor(preprocessor)
    options.SetClearAfterRollback(True)
    transaction.SetFailureHandlingOptions(options)


def assign_workset(element, workset_id):
    param = element.get_Parameter(BuiltInParameter.ELEM_PARTITION_PARAM)
    if param is not None and not param.IsReadOnly:
        try:
            param.Set(workset_id.IntegerValue)
            return True
        except Exception:
            return False
    return False


def error_summary_line(traceback_text):
    """Pulls just the final exception line out of a full traceback string,
    e.g. 'ValueError: No level named ...' instead of the whole stack."""
    lines = [line for line in traceback_text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else traceback_text.strip()


def find_duplicate_resolution_targets(resolution):
    """Flags host levels that more than one distinct linked (link, level)
    pair got resolved onto — worth a glance, since if their footprints also
    happen to coincide this is what causes shafts to auto-merge."""
    target_counts = Counter(host_level.Id.IntegerValue for host_level, _diff, _method, _source in resolution.values())
    dupes = {}
    for key, (host_level, _diff, _method, _source) in resolution.items():
        host_id = host_level.Id.IntegerValue
        if target_counts[host_id] > 1:
            dupes.setdefault(element_name(host_level), []).append(key)
    return dupes


def is_element_still_valid(element):
    """Guards against elements that Revit silently merges/deletes during
    regeneration (e.g. two shafts with identical overlapping boundaries on
    the same levels get consolidated into one opening automatically)."""
    try:
        return element is not None and element.IsValidObject
    except Exception:
        return False


def collect_required_levels(shaft_jobs):
    """Every distinct (link_instance, linked Level element) pair referenced
    by Base/Top Constraint across the shafts about to be recreated (skips
    'Unconnected' tops), deduplicated by (link instance id, level id)."""
    seen_keys = set()
    required = []
    for link_instance, linked_opening in shaft_jobs:
        linked_doc = linked_opening.Document
        for param_name in (PARAM_BASE_CONSTRAINT, PARAM_TOP_CONSTRAINT):
            level_id = get_level_param_id(linked_opening, param_name)
            if level_id is None or level_id == ElementId.InvalidElementId:
                continue
            level_id_value = level_id.IntegerValue if hasattr(level_id, "IntegerValue") else level_id.Value
            key = (link_instance.Id.IntegerValue, level_id_value)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            level = linked_doc.GetElement(level_id)
            if level is not None:
                required.append((link_instance, level))
    return required


def build_level_resolution_map(host_doc, shaft_jobs):
    """Resolves every linked level actually needed by shaft_jobs to a host
    Level, keyed by (link_instance_id, linked_level_id) so the same physical
    level is only ever resolved once, even if referenced by many shafts or
    shares a name with an unrelated level in another link.

    Unlike a plain nearest-match, this doesn't require the host level to be
    physically close: it always picks the closest available host level as
    the reference, and records the residual vertical distance (diff_ft) so
    the caller can fold that into the shaft's Base/Top Offset. The result is
    that a linked level with no host equivalent at all (e.g. linked '1st
    Floor' at 100mm with the host only having '2nd Floor' at 3100mm) still
    lands the shaft at the exact original elevation — just referenced off
    '2nd Floor' with an offset of -3000mm instead of a level of its own.

    Resolution order per linked level:
        1. Exact name match against a host level (diff_ft still computed,
           normally ~0 for a true match).
        2. Nearest host level by elevation, transformed through that link
           instance's specific placement transform into host coordinates.
        3. Manual pick, only if the transform/elevation lookup itself fails
           or the host model has no levels at all - shown with the linked
           level's approximate host-coordinate elevation to help judge.

    Returns {(link_id, level_id): (host_level, diff_ft, method_text, source_label)}
    where diff_ft = (linked level's elevation in host coordinates) - host_level.Elevation.
    """
    host_levels_by_name = collect_levels_by_name(host_doc)
    all_host_levels = list(host_levels_by_name.values())

    resolution = {}
    manual_needed = []

    for link_instance, linked_level in collect_required_levels(shaft_jobs):
        level_id = linked_level.Id
        level_id_value = level_id.IntegerValue if hasattr(level_id, "IntegerValue") else level_id.Value
        key = (link_instance.Id.IntegerValue, level_id_value)
        linked_name = element_name(linked_level)
        source_label = "{0} : {1}".format(element_name(link_instance), linked_name)

        target_z = None
        try:
            transform = link_instance.GetTotalTransform()
            target_z = transform.OfPoint(XYZ(0, 0, linked_level.Elevation)).Z
        except Exception:
            pass

        if target_z is None or not all_host_levels:
            manual_needed.append((key, link_instance, linked_level, target_z, source_label))
            continue

        name_match = host_levels_by_name.get(linked_name)
        if name_match is not None:
            resolution[key] = (name_match, target_z - name_match.Elevation, "name match", source_label)
            continue

        nearest_level = min(all_host_levels, key=lambda lvl: abs(lvl.Elevation - target_z))
        diff_ft = target_z - nearest_level.Elevation
        resolution[key] = (
            nearest_level,
            diff_ft,
            "nearest level, {0:+.0f} mm offset".format(diff_ft * MM_PER_FOOT),
            source_label,
        )

    for key, link_instance, linked_level, target_z, source_label in manual_needed:
        label_to_level = {}
        for level in all_host_levels:
            try:
                elev_mm = level.Elevation * MM_PER_FOOT
                label = "{0}  (Elev {1:.0f} mm)".format(element_name(level), elev_mm)
            except Exception:
                label = element_name(level)
            label_to_level[label] = level
        sorted_labels = sorted(label_to_level.keys())

        target_desc = ""
        if target_z is not None:
            target_desc = " Linked level sits at approx. {0:.0f} mm in host coordinates.".format(
                target_z * MM_PER_FOOT
            )

        chosen_label = forms.SelectFromList.show(
            sorted_labels,
            title="Could not auto-resolve '{0}' (from {1}).{2} Pick the host level to reference:".format(
                element_name(linked_level), element_name(link_instance), target_desc
            ),
            button_name="Use This Level",
            multiselect=False,
        )
        if chosen_label:
            host_level = label_to_level[chosen_label]
            diff_ft = (target_z - host_level.Elevation) if target_z is not None else 0.0
            resolution[key] = (host_level, diff_ft, "manual", source_label)

    return resolution


def prompt_for_links(link_instances):
    """Shows a multi-select list of loaded links.

    Returns a list of chosen RevitLinkInstance (possibly more than one), or
    None if cancelled / nothing picked.
    """
    # Id suffix disambiguates multiple instances of the same linked file.
    options = {}
    for link_instance in link_instances:
        link_doc = link_instance.GetLinkDocument()
        status = "loaded" if link_doc is not None else "NOT LOADED"
        label = "{0}  [Id {1}, {2}]".format(
            element_name(link_instance),
            link_instance.Id.IntegerValue if hasattr(link_instance.Id, "IntegerValue") else link_instance.Id.Value,
            status,
        )
        options[label] = link_instance

    chosen_labels = forms.SelectFromList.show(
        sorted(options.keys()),
        title="Create Shaft Openings from Link",
        button_name="Use These Links",
        multiselect=True,
    )

    if not chosen_labels:
        return None

    return [options[label] for label in chosen_labels]


def get_boundary_curve_array(opening):
    """Returns a CurveArray for the opening's plan boundary, trying the
    approaches that exist across different Revit API versions in order:

    1. Opening.BoundaryLines - present on newer Revit API builds only.
    2. Opening.BoundaryRect - two opposite corner points, for shafts drawn
       with the rectangle sketch tool; rebuilt into 4 Lines here.
    3. The underlying Sketch element's Profile - present for any sketch-based
       opening; takes the first (outer) loop only, so shafts with an island/
       donut profile will only recreate the outer boundary.
    """
    boundary_lines = None
    try:
        boundary_lines = opening.BoundaryLines
    except AttributeError:
        boundary_lines = None

    if boundary_lines is not None and not boundary_lines.IsEmpty:
        return boundary_lines

    if getattr(opening, "IsRectBoundary", False):
        corners = list(opening.BoundaryRect)
        if len(corners) >= 2:
            p0, p1 = corners[0], corners[1]
            z = p0.Z
            x0, x1 = min(p0.X, p1.X), max(p0.X, p1.X)
            y0, y1 = min(p0.Y, p1.Y), max(p0.Y, p1.Y)
            pts = [XYZ(x0, y0, z), XYZ(x1, y0, z), XYZ(x1, y1, z), XYZ(x0, y1, z)]
            rect = CurveArray()
            for i in range(4):
                rect.Append(Line.CreateBound(pts[i], pts[(i + 1) % 4]))
            return rect

    sketch_id = getattr(opening, "SketchId", None)
    if sketch_id is not None and sketch_id != ElementId.InvalidElementId:
        sketch = opening.Document.GetElement(sketch_id)
        profile = getattr(sketch, "Profile", None) if sketch is not None else None
        if profile is not None and profile.Size > 0:
            return profile.get_Item(0)

    return None


def create_shaft_from_linked_opening(host_doc, link_instance, linked_opening, level_resolution=None):
    """Recreates a linked Shaft Opening inside host_doc, transformed into
    host coordinates, matching base/top constraints and offsets.

    Must be called inside an open Transaction on host_doc.

    Args:
        host_doc: the current (host) Document to create the opening in.
        link_instance: RevitLinkInstance that contains linked_opening.
        linked_opening: the Opening element (OST_ShaftOpening) from the link.
        level_resolution: dict {(link_instance_id, linked_level_id): (host_level, diff_ft, method_text, source_label)}
            as built by build_level_resolution_map(). diff_ft is folded into
            the shaft's own Base/Top Offset so it lands at the exact
            original elevation even when host_level isn't at that elevation.
            Required for any shaft whose Base/Top Constraint level doesn't
            share an exact name with a host level.

    Returns:
        the newly created Opening element in host_doc.

    Raises:
        ValueError if the boundary or a resolved host level cannot be found.
    """
    if not is_shaft_opening(linked_opening):
        raise ValueError("Source element is not a Shaft Opening.")

    boundary = get_boundary_curve_array(linked_opening)
    if boundary is None or boundary.IsEmpty:
        raise ValueError("Could not read a boundary for this Shaft Opening via BoundaryLines, BoundaryRect, or its Sketch profile.")

    transform = link_instance.GetTotalTransform()

    transformed_boundary = CurveArray()
    for curve in boundary:
        transformed_boundary.Append(curve.CreateTransformed(transform))

    level_resolution = level_resolution or {}
    link_id_value = link_instance.Id.IntegerValue if hasattr(link_instance.Id, "IntegerValue") else link_instance.Id.Value

    def resolve_host_level(linked_level_id):
        """Returns (host_level, diff_ft) where diff_ft is the extra offset
        (in feet) needed on top of the shaft's own Base/Top Offset so it
        still lands at the exact original elevation, even though host_level
        itself may not sit at that elevation."""
        if linked_level_id is None or linked_level_id == ElementId.InvalidElementId:
            return None, 0.0
        level_id_value = (
            linked_level_id.IntegerValue if hasattr(linked_level_id, "IntegerValue") else linked_level_id.Value
        )
        entry = level_resolution.get((link_id_value, level_id_value))
        if entry is None:
            linked_level = linked_opening.Document.GetElement(linked_level_id)
            raise ValueError(
                "No resolved host level for linked level '{0}' (from {1}).".format(
                    element_name(linked_level), element_name(link_instance)
                )
            )
        host_level, diff_ft = entry[0], entry[1]
        return host_level, diff_ft

    base_level_id = get_level_param_id(linked_opening, PARAM_BASE_CONSTRAINT)
    top_level_id = get_level_param_id(linked_opening, PARAM_TOP_CONSTRAINT)

    host_base_level, base_diff_ft = resolve_host_level(base_level_id)
    if host_base_level is None:
        raise ValueError("Could not resolve the shaft's Base Constraint level.")

    top_is_unconnected = top_level_id is None or top_level_id == ElementId.InvalidElementId
    if top_is_unconnected:
        host_top_level, top_diff_ft = host_base_level, 0.0
    else:
        host_top_level, top_diff_ft = resolve_host_level(top_level_id)

    new_opening = host_doc.Create.NewOpening(host_base_level, host_top_level, transformed_boundary)

    raw_base_offset = get_double_param(linked_opening, PARAM_BASE_OFFSET) or 0.0
    set_double_param(new_opening, PARAM_BASE_OFFSET, raw_base_offset + base_diff_ft)

    if top_is_unconnected:
        set_level_param_id(new_opening, PARAM_TOP_CONSTRAINT, ElementId.InvalidElementId)
        set_double_param(
            new_opening,
            PARAM_UNCONNECTED_HEIGHT,
            get_double_param(linked_opening, PARAM_UNCONNECTED_HEIGHT),
        )
    else:
        raw_top_offset = get_double_param(linked_opening, PARAM_TOP_OFFSET) or 0.0
        set_double_param(new_opening, PARAM_TOP_OFFSET, raw_top_offset + top_diff_ft)

    return new_opening


def main():
    if not doc.IsWorkshared:
        TaskDialog.Show(
            "Create Shaft Openings from Link",
            "This model is not workshared, so the '{0}' workset cannot be used. Enable worksharing first.".format(
                COPIED_SHAFT_WORKSET_NAME
            ),
        )
        return

    link_instances = get_link_instances(doc)
    if not link_instances:
        TaskDialog.Show("Create Shaft Openings from Link", "No Revit links found in this model.")
        return

    chosen_links = prompt_for_links(link_instances)
    if not chosen_links:
        return

    # (link_instance, linked_opening) pairs pooled across every chosen link.
    shaft_jobs = []
    unloaded_links = []
    empty_links = []

    for link_instance in chosen_links:
        link_doc = link_instance.GetLinkDocument()
        if link_doc is None:
            unloaded_links.append(element_name(link_instance))
            continue

        link_shafts = collect_shaft_openings(link_doc)
        if not link_shafts:
            empty_links.append(element_name(link_instance))
            continue

        for linked_opening in link_shafts:
            shaft_jobs.append((link_instance, linked_opening))

    if not shaft_jobs:
        TaskDialog.Show(
            "Create Shaft Openings from Link",
            "No usable Shaft Openings found across the selected link(s).",
        )
        return

    level_resolution = build_level_resolution_map(doc, shaft_jobs)

    created = []
    errors = []
    deleted_count = 0
    locked_shaft_owners = []  # [(element_id, owner_username)]
    workset_created = False

    transaction_group = TransactionGroup(doc, "Create Shaft Openings from Link")
    transaction_group.Start()
    try:
        setup_transaction = Transaction(doc, "Prepare copied shaft openings")
        setup_transaction.Start()
        setup_failures = ShaftFailuresPreprocessor()
        set_failure_handling(setup_transaction, setup_failures)

        workset, workset_created = get_or_create_workset(doc, COPIED_SHAFT_WORKSET_NAME)

        old_shaft_ids_all = collect_host_shafts_in_workset(doc, workset.Id)
        old_shaft_ids = []
        for shaft_id in old_shaft_ids_all:
            owner = get_other_user_owner(doc, shaft_id)
            if owner:
                locked_shaft_owners.append((shaft_id, owner))
            else:
                old_shaft_ids.append(shaft_id)

        if old_shaft_ids:
            doc.Delete(List[ElementId](old_shaft_ids))
            deleted_count = len(old_shaft_ids)

        setup_status = setup_transaction.Commit()
        if setup_status != TransactionStatus.Committed:
            raise RuntimeError(
                "Could not prepare the target workset: {0}".format(
                    "; ".join(setup_failures.error_messages) or "Revit rolled back the setup transaction."
                )
            )

        for link_instance, linked_opening in shaft_jobs:
            linked_id = (
                linked_opening.Id.IntegerValue
                if hasattr(linked_opening.Id, "IntegerValue")
                else linked_opening.Id.Value
            )
            shaft_transaction = Transaction(doc, "Create linked shaft {0}".format(linked_id))
            shaft_transaction.Start()
            shaft_failures = ShaftFailuresPreprocessor()
            set_failure_handling(shaft_transaction, shaft_failures)
            try:
                new_opening = create_shaft_from_linked_opening(
                    doc, link_instance, linked_opening, level_resolution=level_resolution
                )
                assign_workset(new_opening, workset.Id)
                commit_status = shaft_transaction.Commit()
                if commit_status == TransactionStatus.Committed:
                    created.append((link_instance, linked_opening, new_opening))
                else:
                    errors.append(
                        (
                            link_instance,
                            linked_opening,
                            "Revit rolled back this shaft:\n{0}".format(
                                "\n".join(shaft_failures.error_messages)
                                or "The shaft sketch or its constraints are invalid in the host model."
                            ),
                        )
                    )
            except Exception:
                err_text = traceback.format_exc()
                try:
                    if shaft_transaction.HasStarted() and not shaft_transaction.HasEnded():
                        shaft_transaction.RollBack()
                except Exception:
                    pass
                errors.append((link_instance, linked_opening, err_text))

        transaction_group.Assimilate()
    except Exception:
        try:
            if transaction_group.HasStarted() and not transaction_group.HasEnded():
                transaction_group.RollBack()
        except Exception:
            pass
        TaskDialog.Show("Create Shaft Openings from Link", "Setup failed:\n{0}".format(traceback.format_exc()))
        return

    valid_created = []
    merged_count = 0
    for link_instance, linked_opening, new_opening in created:
        if is_element_still_valid(new_opening):
            valid_created.append((link_instance, linked_opening, new_opening))
        else:
            merged_count += 1
    created = valid_created

    output.print_md("# Create Shaft Openings from Link")
    try:
        output.show()
    except Exception:
        pass
    source_names = ", ".join(sorted(set(element_name(li) for li, _ in shaft_jobs))) if shaft_jobs else ""
    output.print_md(
        "Source link(s): **{0}**. Found **{1}** shaft opening(s) total. Workset **{2}**{3}. Deleted **{4}** previous shaft(s). Created **{5}**, **{6}** error(s).".format(
            source_names,
            len(shaft_jobs),
            COPIED_SHAFT_WORKSET_NAME,
            " (newly created, hidden by default in all views)" if workset_created else "",
            deleted_count,
            len(created),
            len(errors),
        )
    )

    if unloaded_links:
        output.print_md("\n**Skipped unloaded link(s):** {0}".format(", ".join(unloaded_links)))
    if empty_links:
        output.print_md("**Link(s) with no shaft openings:** {0}".format(", ".join(empty_links)))
    if locked_shaft_owners:
        owner_counts = Counter(owner for _id, owner in locked_shaft_owners)
        owner_summary = ", ".join("{0} ({1})".format(owner, count) for owner, count in owner_counts.items())
        output.print_md(
            "**{0}** old shaft(s) on the workset were skipped because they're currently checked out: {1}. "
            "Ask them to Sync to Central (which relinquishes ownership), then Reload Latest and re-run to pick those up.".format(
                len(locked_shaft_owners), owner_summary
            )
        )
    if merged_count:
        output.print_md(
            "**{0}** created shaft(s) were auto-merged by Revit into other overlapping shafts during regeneration (identical footprint on the same levels) and are not listed individually below.".format(
                merged_count
            )
        )
    if level_resolution:
        output.print_md("\n**Level resolution ({0} distinct linked level(s)):**".format(len(level_resolution)))
        for _key, (host_level, _diff_ft, method, source_label) in sorted(
            level_resolution.items(), key=lambda item: (item[1][2], item[1][3])
        ):
            output.print_md("- {0} → **{1}** ({2})".format(source_label, element_name(host_level), method))

    dupes = find_duplicate_resolution_targets(level_resolution)
    if dupes:
        output.print_md("\n**Note:** more than one linked level resolved to the same host level:")
        for host_name, keys in sorted(dupes.items()):
            output.print_md("- **{0}** ← {1} linked level(s)".format(host_name, len(keys)))

    if created:
        output.print_table(
            table_data=[
                [
                    element_name(link_instance),
                    element_name(linked) or "(unnamed)",
                    str(linked.Id.IntegerValue if hasattr(linked.Id, "IntegerValue") else linked.Id.Value),
                    str(new.Id.IntegerValue if hasattr(new.Id, "IntegerValue") else new.Id.Value),
                    output.linkify(new.Id),
                ]
                for link_instance, linked, new in created
            ],
            columns=["Source Link", "Linked Shaft", "Linked Id", "New Host Id", "Select"],
        )

    if errors:
        output.print_md("\n## Errors ({0})".format(len(errors)))
        for link_instance, linked, err in errors:
            output.print_md(
                "**{0} — Linked Shaft Id {1}:**\n```\n{2}\n```".format(
                    element_name(link_instance),
                    linked.Id.IntegerValue if hasattr(linked.Id, "IntegerValue") else linked.Id.Value,
                    err,
                )
            )

    result_dialog = TaskDialog("Create Shaft Openings from Link")
    result_dialog.MainIcon = (
        TaskDialogIcon.TaskDialogIconInformation if not errors else TaskDialogIcon.TaskDialogIconWarning
    )
    result_dialog.MainInstruction = "Created {0} of {1} shaft opening(s).".format(len(created), len(shaft_jobs))
    result_dialog.MainContent = "From {0} link(s). Workset '{1}': deleted {2}, created {3}. {4} error(s).{5}{6}".format(
        len(chosen_links),
        COPIED_SHAFT_WORKSET_NAME,
        deleted_count,
        len(created),
        len(errors),
        " {0} auto-merged by Revit.".format(merged_count) if merged_count else "",
        " {0} skipped (checked out by another user).".format(len(locked_shaft_owners)) if locked_shaft_owners else "",
    )

    if errors:
        error_counts = Counter(error_summary_line(err) for _, _, err in errors)
        top_lines = [
            "{0}x  {1}".format(count, message) for message, count in error_counts.most_common(5)
        ]
        result_dialog.ExpandedContent = "Most common error(s):\n\n" + "\n\n".join(top_lines)

    result_dialog.Show()


if __name__ == "__main__":
    main()
