# -*- coding: utf-8 -*-
__title__ = "Room Parameter\nUpdate"
__author__ = "JM"
__doc__ = """Version = 5.0
Date    = 2026-07-27

Description:
Click-through room parameter updater - sequence or match mode.

Set up once, then click rooms one at a time in the model to apply
the update. Three modes are available:

- Rename by Sequence: choose a prefix, a start number, and one or more
  target parameters. Each click writes "{prefix} {n}" (e.g. "FL 6",
  "FL 7", "FL 8"...) into every selected target parameter
  and advances the counter.

- Match Parameter: choose ONE OR MORE parameters up front (e.g.
  Name), then click ONE base room - that room's values for
  each chosen parameter are captured once. Every room you click after
  that has ALL of those SAME parameters set to their captured base
  values - i.e. all clicked rooms are matched to the base room,
  not to themselves.

- Match All Parameters (Exact): click ONE base room. Every writable
  parameter on that room is captured exactly (by storage type - text,
  number, integer, or element/type reference), EXCEPT Number, which
  is always left alone since it must stay unique per room. Every
  room you click after that has all of those (non-Number)
  parameters overwritten to match the base room exactly, the same
  way Revit's native Match Type Properties works but for room
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

# Number must stay unique per room - never overwritten by Match All.
MATCH_ALL_EXCLUDED_PARAMS = set(["Number"])


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


def is_room(element):
    try:
        return (
            element is not None
            and element.Category is not None
            and element.Category.Id.IntegerValue == int(BuiltInCategory.OST_Rooms)
        )
    except Exception:
        return False


class RoomSelectionFilter(ISelectionFilter):
    def AllowElement(self, element):
        return is_room(element)

    def AllowReference(self, reference, point):
        return False


def show_info(instruction, content=None, icon=TaskDialogIcon.TaskDialogIconInformation):
    dialog = TaskDialog("Room Parameter Update")
    dialog.MainIcon = icon
    dialog.MainInstruction = instruction
    if content:
        dialog.MainContent = content
    dialog.Show()


def prompt_mode():
    return forms.CommandSwitchWindow.show(
        [MODE_SEQUENCE, MODE_MATCH, MODE_MATCH_ALL],
        message="Choose how room parameters should be updated.",
    )


def prompt_prefix():
    value = forms.ask_for_string(
        default="",
        prompt="Enter the prefix to use.",
        title="Room Sequence Rename",
    )
    if value is None:
        return None
    return value.strip()


def prompt_start_number():
    value = forms.ask_for_string(
        default="",
        prompt="Enter the sequence start number.",
        title="Room Sequence Rename",
    )
    if value is None:
        return None

    try:
        start_number = int(value)
    except Exception:
        show_info("Invalid start number: {0}".format(value), icon=TaskDialogIcon.TaskDialogIconWarning)
        return None

    return start_number


def get_sample_room():
    return (
        FilteredElementCollector(doc)
        .OfCategory(BuiltInCategory.OST_Rooms)
        .WhereElementIsNotElementType()
        .FirstElement()
    )


def get_writable_params(room):
    param_names = []
    seen = set()
    for param in room.Parameters:
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
    default_choice = ["Name"] if "Name" in param_names else param_names[:1]
    selected = forms.SelectFromList.show(
        param_names,
        title="Room Parameter Update",
        button_name="Select Target Parameter(s)",
        multiselect=True,
        default=default_choice,
    )
    return selected


def prompt_match_parameters(param_names):
    default_choice = ["Name"] if "Name" in param_names else param_names[:1]
    selected = forms.SelectFromList.show(
        param_names,
        title="Room Parameter Update",
        button_name="Select Parameter(s) to Match",
        multiselect=True,
        default=default_choice,
    )
    return selected


def build_sequence_value(prefix, number):
    if prefix:
        return "{0} {1}".format(prefix, number)
    return safe_str(number)


def get_param_value_text(room, parameter_name):
    param = room.LookupParameter(parameter_name)
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


def copy_all_params(source_room, target_room):
    updated_params = []
    param_skips = []

    for source_param in source_room.Parameters:
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

            target_param = target_room.LookupParameter(name)
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


def pick_room(prompt_text):
    try:
        reference = uidoc.Selection.PickObject(
            ObjectType.Element,
            RoomSelectionFilter(),
            prompt_text,
        )
    except Exception:
        # User pressed Esc / cancelled the pick.
        return None

    if reference is None:
        return None

    element = doc.GetElement(reference.ElementId)
    if not is_room(element):
        return None

    return element


def identity_text(element):
    try:
        param = element.LookupParameter("Number")
        if param is not None:
            return safe_str(param.AsString())
    except Exception:
        pass
    return ""


def main():
    mode = prompt_mode()
    if mode is None:
        return

    sample_room = get_sample_room()
    if sample_room is None:
        show_info("No rooms were found in the project to read parameters from.")
        return

    prefix = None
    start_number = None
    match_parameter_names = None
    base_room = None
    base_values = None
    parameter_names = None

    if mode == MODE_SEQUENCE:
        prefix = prompt_prefix()
        if prefix is None:
            return

        start_number = prompt_start_number()
        if start_number is None:
            return

        target_param_names = get_writable_params(sample_room)
        if not target_param_names:
            show_info("No writable parameters were found on rooms in this project.")
            return

        parameter_names = prompt_target_parameters(target_param_names)
        if not parameter_names:
            return

    elif mode == MODE_MATCH:
        # Preselect the parameter(s) to match BEFORE picking any rooms.
        # These same parameters are both read from the base room and
        # written to every subsequently clicked room.
        writable_param_names = get_writable_params(sample_room)
        if not writable_param_names:
            show_info("No writable parameters were found on rooms in this project.")
            return

        match_parameter_names = prompt_match_parameters(writable_param_names)
        if not match_parameter_names:
            return

        # Now pick the ONE base room to read the values from.
        base_room = pick_room(
            "Click the BASE room to match from ({0}).".format(", ".join(match_parameter_names))
        )
        if base_room is None:
            return

        base_values = {}
        for parameter_name in match_parameter_names:
            value = get_param_value_text(base_room, parameter_name)
            if value is not None:
                base_values[parameter_name] = value

        if not base_values:
            show_info(
                "The base room has no value for any of the selected parameters.",
                icon=TaskDialogIcon.TaskDialogIconWarning,
            )
            return

    else:
        # MODE_MATCH_ALL - no parameter selection at all, just pick the
        # base room; every writable parameter gets copied exactly, except
        # Number, which must stay unique per room.
        base_room = pick_room("Click the BASE room to match ALL parameters from.")
        if base_room is None:
            return
        parameter_names = [
            name for name in get_writable_params(base_room)
            if name not in MATCH_ALL_EXCLUDED_PARAMS
        ]
        if not parameter_names:
            show_info("The base room has no writable parameters to copy.")
            return

    results = []
    skipped = []
    current_number = start_number

    transaction = Transaction(doc, "Room Parameter Update")
    transaction.Start()

    try:
        while True:
            if mode == MODE_MATCH:
                element = pick_room(
                    "Click a room to match to the base room ({0}), or Esc to finish.".format(
                        ", ".join(match_parameter_names)
                    )
                )
            elif mode == MODE_MATCH_ALL:
                element = pick_room(
                    "Click a room to match ALL parameters to the base room, or Esc to finish."
                )
            else:
                element = pick_room("Click a room to apply the next sequence value, or Esc to finish.")

            if element is None:
                break

            if mode in (MODE_MATCH, MODE_MATCH_ALL) and element.Id == base_room.Id:
                # Skip re-applying onto the base room itself.
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
                updated_params, param_skips = copy_all_params(base_room, element)
                for param_name, reason in param_skips:
                    skipped.append((element, param_name, "(base value)", reason))
                if updated_params:
                    results.append((element, "(matched to base)", ", ".join(updated_params)))

        transaction.Commit()
    except Exception:
        transaction.RollBack()
        show_info(
            "Room Parameter Update failed.",
            safe_str(traceback.format_exc()),
            icon=TaskDialogIcon.TaskDialogIconError,
        )
        return

    output.print_md("# Room Parameter Update")
    if mode == MODE_SEQUENCE:
        output.print_md(
            "Mode: **Rename by Sequence** | Targets: **{0}** | Prefix: **{1}** | Start: **{2}**".format(
                ", ".join(parameter_names), prefix if prefix else "(none)", start_number
            )
        )
    elif mode == MODE_MATCH:
        output.print_md(
            "Mode: **Match Parameter** | Matched Parameters: **{0}** | Base Room: **{1}** (Id {2})".format(
                ", ".join(match_parameter_names), identity_text(base_room), output.linkify(base_room.Id)
            )
        )
    else:
        output.print_md(
            "Mode: **Match All Parameters (Exact)** | Base Room: **{0}** (Id {1}) | Parameters Considered: **{2}**".format(
                identity_text(base_room), output.linkify(base_room.Id), len(parameter_names)
            )
        )

    if results:
        output.print_md("\n## Updated ({0} room(s))".format(len(results)))
        output.print_table(
            table_data=[
                [output.linkify(element.Id), identity_text(element), new_value, params_text]
                for element, new_value, params_text in results
            ],
            columns=["Room Id", "Existing Number", "New Value", "Parameters Updated"],
        )

    if skipped:
        output.print_md("\n## Skipped ({0})".format(len(skipped)))
        output.print_table(
            table_data=[
                [output.linkify(element.Id), identity_text(element), parameter_name, new_value, reason]
                for element, parameter_name, new_value, reason in skipped
            ],
            columns=["Room Id", "Existing Number", "Parameter", "Intended Value", "Reason"],
        )

    show_info(
        "Room Parameter Update complete.",
        "{0} room(s) updated, {1} parameter update(s) skipped.".format(len(results), len(skipped)),
    )


if __name__ == "__main__":
    main()