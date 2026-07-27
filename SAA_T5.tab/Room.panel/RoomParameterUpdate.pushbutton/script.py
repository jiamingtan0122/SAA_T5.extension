# -*- coding: utf-8 -*-
__title__ = "Room Parameter\nUpdate"
__author__ = "JM"
__doc__ = """Version = 1.0
Date    = 2026-07-27

Description:
Click-through room parameter updater - sequence or match mode.

Set up once, then click rooms one at a time in the model to apply the
update. Two modes are available:

- Rename by Sequence: choose a prefix, a start number, and one or more
  target parameters. Each click writes "{prefix} {n}" (e.g. "FL 6",
  "FL 7", "FL 8"...) into every selected target parameter and advances
  the counter.

- Match Parameter: choose ONE parameter up front (e.g. Name), then
  click ONE base room - that room's value for the chosen parameter is
  captured once. Every room you click after that has the SAME
  parameter set to that captured value - i.e. all clicked rooms are
  matched to the base room, not to themselves.

- Match All Parameters (Exact): click ONE base room. Every writable
  parameter on that room is captured exactly (by storage type - text,
  number, integer, or element/type reference), EXCEPT Room Number,
  which is always left alone since it must stay unique per room. Every
  room you click after that has all of those (non-Number) parameters
  overwritten to match the base room exactly, the same way Revit's
  native Match Type Properties works but for room instance parameters.

Either way, picking keeps going until Esc / right-click > Cancel is
used to finish.
"""

from Autodesk.Revit.DB import (
    BuiltInCategory,
    FilteredElementCollector,
    StorageType,
    Transaction,
)
from Autodesk.Revit.DB.Architecture import Room
from Autodesk.Revit.UI import TaskDialog, TaskDialogIcon
from Autodesk.Revit.UI.Selection import ISelectionFilter, ObjectType

from pyrevit import forms, revit, script

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

MODE_SEQUENCE = "Rename by Sequence"
MODE_MATCH = "Match Parameter"
MODE_MATCH_ALL = "Match All Parameters (Exact)"

# Room Number must stay unique per room - never overwritten by Match All.
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
            isinstance(element, Room)
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


def get_all_param_names(room):
    param_names = []
    seen = set()
    for param in room.Parameters:
        try:
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


def prompt_source_parameter(param_names):
    default_choice = "Name" if "Name" in param_names else param_names[0]
    selected = forms.SelectFromList.show(
        param_names,
        title="Room Parameter Update",
        button_name="Select Source Parameter",
        multiselect=False,
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

    room = doc.GetElement(reference.ElementId)
    if not is_room(room):
        return None

    return room


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
    source_parameter_name = None
    base_room = None
    base_value = None

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
        # Preselect the single parameter to match BEFORE picking any rooms.
        # This same parameter is both read from the base room and written
        # to every subsequently clicked room.
        writable_param_names = get_writable_params(sample_room)
        if not writable_param_names:
            show_info("No writable parameters were found on rooms in this project.")
            return

        source_parameter_name = prompt_source_parameter(writable_param_names)
        if source_parameter_name is None:
            return

        parameter_names = [source_parameter_name]

        # Now pick the ONE base room to read the value from.
        base_room = pick_room(
            "Click the BASE room to match from (parameter: {0}).".format(source_parameter_name)
        )
        if base_room is None:
            return

        base_value = get_param_value_text(base_room, source_parameter_name)
        if base_value is None:
            show_info(
                "The base room has no value for parameter '{0}'.".format(source_parameter_name),
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
                room = pick_room(
                    "Click a room to match to base '{0}' ({1}), or Esc to finish.".format(
                        base_value, source_parameter_name
                    )
                )
            elif mode == MODE_MATCH_ALL:
                room = pick_room(
                    "Click a room to match ALL parameters to the base room, or Esc to finish."
                )
            else:
                room = pick_room("Click a room to apply the next sequence value, or Esc to finish.")

            if room is None:
                break

            if mode in (MODE_MATCH, MODE_MATCH_ALL) and room.Id == base_room.Id:
                # Skip re-applying onto the base room itself.
                continue

            if mode == MODE_SEQUENCE:
                new_value = build_sequence_value(prefix, current_number)
                current_number += 1
                updated_params = []
                for parameter_name in parameter_names:
                    param = room.LookupParameter(parameter_name)
                    if param is None or param.IsReadOnly:
                        skipped.append((room, parameter_name, new_value, "Parameter unavailable or read-only"))
                        continue
                    try:
                        param.Set(new_value)
                        updated_params.append(parameter_name)
                    except Exception:
                        skipped.append((room, parameter_name, new_value, safe_str(traceback.format_exc())))

                if updated_params:
                    results.append((room, new_value, ", ".join(updated_params)))

            elif mode == MODE_MATCH:
                new_value = base_value
                updated_params = []
                for parameter_name in parameter_names:
                    param = room.LookupParameter(parameter_name)
                    if param is None or param.IsReadOnly:
                        skipped.append((room, parameter_name, new_value, "Parameter unavailable or read-only"))
                        continue
                    try:
                        param.Set(new_value)
                        updated_params.append(parameter_name)
                    except Exception:
                        skipped.append((room, parameter_name, new_value, safe_str(traceback.format_exc())))

                if updated_params:
                    results.append((room, new_value, ", ".join(updated_params)))

            else:
                # MODE_MATCH_ALL
                updated_params, param_skips = copy_all_params(base_room, room)
                for param_name, reason in param_skips:
                    skipped.append((room, param_name, "(base value)", reason))
                if updated_params:
                    results.append((room, "(matched to base)", ", ".join(updated_params)))

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
            "Mode: **Match Parameter** | Source: **{0}** | Base Room: **{1}** (Id {2}) | Base Value: **{3}** | Targets: **{4}**".format(
                source_parameter_name, safe_str(base_room.Number), output.linkify(base_room.Id),
                base_value, ", ".join(parameter_names)
            )
        )
    else:
        output.print_md(
            "Mode: **Match All Parameters (Exact)** | Base Room: **{0}** (Id {1}) | Parameters Considered: **{2}**".format(
                safe_str(base_room.Number), output.linkify(base_room.Id), len(parameter_names)
            )
        )

    if results:
        output.print_md("\n## Updated ({0} room(s))".format(len(results)))
        output.print_table(
            table_data=[
                [output.linkify(room.Id), safe_str(room.Number), new_value, params_text]
                for room, new_value, params_text in results
            ],
            columns=["Room Id", "Existing Room Number", "New Value", "Parameters Updated"],
        )

    if skipped:
        output.print_md("\n## Skipped ({0})".format(len(skipped)))
        output.print_table(
            table_data=[
                [output.linkify(room.Id), safe_str(room.Number), parameter_name, new_value, reason]
                for room, parameter_name, new_value, reason in skipped
            ],
            columns=["Room Id", "Existing Room Number", "Parameter", "Intended Value", "Reason"],
        )

    show_info(
        "Room Parameter Update complete.",
        "{0} room(s) updated, {1} parameter update(s) skipped.".format(len(results), len(skipped)),
    )


if __name__ == "__main__":
    main()