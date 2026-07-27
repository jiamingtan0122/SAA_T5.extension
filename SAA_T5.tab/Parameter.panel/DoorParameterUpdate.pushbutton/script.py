# -*- coding: utf-8 -*-
__title__ = "Door Parameter\nUpdate"
__author__ = "JM"
__doc__ = """Version = 2.0
Date    = 2026-07-27

Description:
Click-through door parameter updater - sequence or match mode.

Set up once, then click doors one at a time in the model to apply
the update. Three modes are available:

- Rename by Sequence: choose a prefix, a start number, and one or more
  target parameters. Each click writes "{prefix} {n}" (e.g. "D 6",
  "D 7", "D 8"...) into every selected target parameter
  and advances the counter.

- Match Parameter: choose ONE OR MORE parameters up front (e.g.
  Comments), then click ONE base door - that door's values for
  each chosen parameter are captured once. Every door you click after
  that has ALL of those SAME parameters set to their captured base
  values - i.e. all clicked doors are matched to the base door,
  not to themselves.

- Match All Parameters (Exact): click ONE base door. Every writable
  parameter on that door is captured exactly (by storage type - text,
  number, integer, or element/type reference), EXCEPT Mark, which
  is always left alone since it must stay unique per door. Every
  door you click after that has all of those (non-Mark)
  parameters overwritten to match the base door exactly, the same
  way Revit's native Match Type Properties works but for door
  instance parameters.

Either way, picking keeps going until Esc / right-click > Cancel is
used to finish.
"""

import traceback

from Autodesk.Revit.DB import (
    BuiltInCategory,
    FilteredElementCollector,
    StorageType,
    Transaction,
)
from Autodesk.Revit.UI import TaskDialog, TaskDialogIcon
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

from pyrevit import forms, revit, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

MODE_SEQUENCE = "Rename by Sequence"
MODE_MATCH = "Match Parameter"
MODE_MATCH_ALL = "Match All Parameters (Exact)"

# Mark must stay unique per door - never overwritten by Match All.
MATCH_ALL_EXCLUDED_PARAMS = set(["Mark"])


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


def is_door(element):
    try:
        return (
            element is not None
            and element.Category is not None
            and element.Category.Id.IntegerValue == int(BuiltInCategory.OST_Doors)
        )
    except Exception:
        return False


class DoorSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return is_door(element)

    def AllowReference(self, reference, point):
        return False


def show_info(instruction, content=None, icon=TaskDialogIcon.TaskDialogIconInformation):
    dialog = TaskDialog("Door Parameter Update")
    dialog.MainIcon = icon
    dialog.MainInstruction = instruction
    if content:
        dialog.MainContent = content
    dialog.Show()


def prompt_mode():
    return forms.CommandSwitchWindow.show(
        [MODE_SEQUENCE, MODE_MATCH, MODE_MATCH_ALL],
        message="Choose how door parameters should be updated.",
    )


def prompt_prefix():
    value = forms.ask_for_string(
        default="",
        prompt="Enter the prefix to use.",
        title="Door Sequence Rename",
    )
    if value is None:
        return None
    return value.strip()


def prompt_start_number():
    value = forms.ask_for_string(
        default="",
        prompt="Enter the sequence start number.",
        title="Door Sequence Rename",
    )
    if value is None:
        return None

    try:
        start_number = int(value)
    except Exception:
        show_info("Invalid start number: {0}".format(value), icon=TaskDialogIcon.TaskDialogIconWarning)
        return None

    return start_number


def get_sample_door():
    return (
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_Doors)
        .WhereElementIsNotElementType()
        .FirstElement()
    )


def get_writable_params(door):
    param_names = []
    seen = set()
    for param in door.Parameters:
        try:
            if param.IsReadOnly:
                continue
            definition = param.Definition
            if definition is None:
                continue
            name = safe_str(definition.Name)
            if not name or name in seen:
                continue
            seen.add(name)
            param_names.append(name)
        except Exception:
            continue
    param_names.sort()
    return param_names


def prompt_target_parameters(param_names):
    default_choice = ["Mark"] if "Mark" in param_names else param_names[:1]
    selected = forms.SelectFromList.show(
        param_names,
        title="Door Parameter Update",
        button_name="Select Target Parameter(s)",
        multiselect=True,
        default=default_choice,
    )
    return selected


def prompt_match_parameters(param_names):
    default_choice = ["Comments"] if "Comments" in param_names else param_names[:1]
    selected = forms.SelectFromList.show(
        param_names,
        title="Door Parameter Update",
        button_name="Select Parameter(s) to Match",
        multiselect=True,
        default=default_choice,
    )
    return selected


def build_sequence_value(prefix, number):
    if prefix:
        return "{0} {1}".format(prefix, number)
    return safe_str(number)


def get_param_value_text(door, parameter_name):
    param = door.LookupParameter(parameter_name)
    if param is None:
        return None

    try:
        value = param.AsValueString()
        if value:
            return safe_str(value)
    except Exception:
        pass

    try:
        value = param.AsString()
        if value:
            return safe_str(value)
    except Exception:
        pass

    try:
        return safe_str(param.AsDouble())
    except Exception:
        return None


def copy_all_params(source_door, target_door):
    updated_params = []
    param_skips = []

    for source_param in source_door.Parameters:
        try:
            if source_param.IsReadOnly:
                continue
            definition = source_param.Definition
            if definition is None:
                continue
            name = safe_str(definition.Name)
            if not name:
                continue
            if name in MATCH_ALL_EXCLUDED_PARAMS:
                continue

            target_param = target_door.LookupParameter(name)
            if target_param is None or target_param.IsReadOnly:
                continue

            storage_type = source_param.StorageType
            if storage_type == StorageType.Double:
                target_param.Set(source_param.AsDouble())
            elif storage_type == StorageType.Integer:
                target_param.Set(source_param.AsInteger())
            elif storage_type == StorageType.String:
                target_param.Set(source_param.AsString() or "")
            elif storage_type == StorageType.ElementId:
                target_param.Set(source_param.AsElementId())
            else:
                continue

            updated_params.append(name)
        except Exception:
            param_skips.append((name, safe_str(traceback.format_exc())))

    return updated_params, param_skips


def pick_door(prompt_text):
    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element,
            DoorSelectionFilter(),
            prompt_text,
        )
    except Exception:
        # User pressed Esc / cancelled the pick.
        return None

    if reference is None:
        return None

    element = doc.GetElement(reference.ElementId)
    if not is_door(element):
        return None

    return element


def identity_text(element):
    try:
        param = element.LookupParameter("Mark")
        if param is not None:
            return safe_str(param.AsString())
    except Exception:
        pass
    return ""


def main():
    mode = prompt_mode()
    if mode is None:
        return

    sample_door = get_sample_door()
    if sample_door is None:
        show_info("No doors were found in the project to read parameters from.")
        return

    prefix = None
    start_number = None
    match_parameter_names = None
    base_door = None
    base_values = None
    parameter_names = None

    if mode == MODE_SEQUENCE:
        prefix = prompt_prefix()
        if prefix is None:
            return

        start_number = prompt_start_number()
        if start_number is None:
            return

        target_param_names = get_writable_params(sample_door)
        if not target_param_names:
            show_info("No writable parameters were found on doors in this project.")
            return

        parameter_names = prompt_target_parameters(target_param_names)
        if not parameter_names:
            return

    elif mode == MODE_MATCH:
        # Preselect the parameter(s) to match BEFORE picking any doors.
        # These same parameters are both read from the base door and
        # written to every subsequently clicked door.
        writable_param_names = get_writable_params(sample_door)
        if not writable_param_names:
            show_info("No writable parameters were found on doors in this project.")
            return

        match_parameter_names = prompt_match_parameters(writable_param_names)
        if not match_parameter_names:
            return

        # Now pick the ONE base door to read the values from.
        base_door = pick_door(
            "Click the BASE door to match from ({0}).".format(", ".join(match_parameter_names))
        )
        if base_door is None:
            return

        base_values = {}
        for parameter_name in match_parameter_names:
            value = get_param_value_text(base_door, parameter_name)
            if value is not None:
                base_values[parameter_name] = value

        if not base_values:
            show_info(
                "The base door has no value for any of the selected parameters.",
                icon=TaskDialogIcon.TaskDialogIconWarning,
            )
            return

    else:
        # MODE_MATCH_ALL - no parameter selection at all, just pick the
        # base door; every writable parameter gets copied exactly, except
        # Mark, which must stay unique per door.
        base_door = pick_door("Click the BASE door to match ALL parameters from.")
        if base_door is None:
            return
        parameter_names = [
            name for name in get_writable_params(base_door)
            if name not in MATCH_ALL_EXCLUDED_PARAMS
        ]
        if not parameter_names:
            show_info("The base door has no writable parameters to copy.")
            return

    results = []
    skipped = []
    current_number = start_number

    transaction = Transaction(doc, "Door Parameter Update")
    transaction.Start()

    try:
        while True:
            if mode == MODE_MATCH:
                element = pick_door(
                    "Click a door to match to the base door ({0}), or Esc to finish.".format(
                        ", ".join(match_parameter_names)
                    )
                )
            elif mode == MODE_MATCH_ALL:
                element = pick_door(
                    "Click a door to match ALL parameters to the base door, or Esc to finish."
                )
            else:
                element = pick_door("Click a door to apply the next sequence value, or Esc to finish.")

            if element is None:
                break

            if mode in (MODE_MATCH, MODE_MATCH_ALL) and element.Id == base_door.Id:
                # Skip re-applying onto the base door itself.
                continue

            if mode == MODE_SEQUENCE:
                new_value = build_sequence_value(prefix, current_number)
                current_number += 1
                updated_params = []
                for parameter_name in parameter_names:
                    param = element.LookupParameter(parameter_name)
                    if param is None or param.IsReadOnly:
                        skipped.append((element, parameter_name, new_value, "Parameter unavailable or read-only"))
                        continue
                    try:
                        param.Set(new_value)
                        updated_params.append(parameter_name)
                    except Exception:
                        skipped.append((element, parameter_name, new_value, safe_str(traceback.format_exc())))

                if updated_params:
                    results.append((element, new_value, ", ".join(updated_params)))

            elif mode == MODE_MATCH:
                updated_params = []
                for parameter_name, value in base_values.items():
                    param = element.LookupParameter(parameter_name)
                    if param is None or param.IsReadOnly:
                        skipped.append((element, parameter_name, value, "Parameter unavailable or read-only"))
                        continue
                    try:
                        param.Set(value)
                        updated_params.append(parameter_name)
                    except Exception:
                        skipped.append((element, parameter_name, value, safe_str(traceback.format_exc())))

                if updated_params:
                    results.append((element, "(matched to base)", ", ".join(updated_params)))

            else:
                # MODE_MATCH_ALL
                updated_params, param_skips = copy_all_params(base_door, element)
                for param_name, reason in param_skips:
                    skipped.append((element, param_name, "(base value)", reason))
                if updated_params:
                    results.append((element, "(matched to base)", ", ".join(updated_params)))

        transaction.Commit()
    except Exception:
        transaction.RollBack()
        show_info(
            "Door Parameter Update failed.",
            safe_str(traceback.format_exc()),
            icon=TaskDialogIcon.TaskDialogIconError,
        )
        return

    output.print_md("# Door Parameter Update")
    if mode == MODE_SEQUENCE:
        output.print_md(
            "Mode: **Rename by Sequence** | Targets: **{0}** | Prefix: **{1}** | Start: **{2}**".format(
                ", ".join(parameter_names), prefix if prefix else "(none)", start_number
            )
        )
    elif mode == MODE_MATCH:
        output.print_md(
            "Mode: **Match Parameter** | Matched Parameters: **{0}** | Base Door: **{1}** (Id {2})".format(
                ", ".join(match_parameter_names), identity_text(base_door), output.linkify(base_door.Id)
            )
        )
    else:
        output.print_md(
            "Mode: **Match All Parameters (Exact)** | Base Door: **{0}** (Id {1}) | Parameters Considered: **{2}**".format(
                identity_text(base_door), output.linkify(base_door.Id), len(parameter_names)
            )
        )

    if results:
        output.print_md("\n## Updated ({0} door(s))".format(len(results)))
        output.print_table(
            table_data=[
                [output.linkify(element.Id), identity_text(element), new_value, params_text]
                for element, new_value, params_text in results
            ],
            columns=["Door Id", "Existing Mark", "New Value", "Parameters Updated"],
        )

    if skipped:
        output.print_md("\n## Skipped ({0})".format(len(skipped)))
        output.print_table(
            table_data=[
                [output.linkify(element.Id), identity_text(element), parameter_name, new_value, reason]
                for element, parameter_name, new_value, reason in skipped
            ],
            columns=["Door Id", "Existing Mark", "Parameter", "Intended Value", "Reason"],
        )

    show_info(
        "Door Parameter Update complete.",
        "{0} door(s) updated, {1} parameter update(s) skipped.".format(len(results), len(skipped)),
    )


if __name__ == "__main__":
    main()