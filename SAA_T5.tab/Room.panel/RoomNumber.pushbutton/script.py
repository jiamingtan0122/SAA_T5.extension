# -*- coding: utf-8 -*-
__title__ = "Room\nNumber"
__author__ = "JK_Sim"
__doc__ = """Version = 1.0
Date    = 30.10.2025
_____________________________________________________________________
Description:

Assigns the next available room "Number" based on format LL-SSSS-FNN
_____________________________________________________________________
How-to:

-> Select ONLY 1 room
-> Click the button
-> Done
_____________________________________________________________________
"""
from pyrevit import forms, revit, script
from Autodesk.Revit.DB import (
    BuiltInCategory,
    SpatialElement,
    RevitLinkInstance,
    FilteredElementCollector,
    Transaction,
)
from System.Text.RegularExpressions import Regex

doc = revit.doc
uidoc = revit.uidoc
output = script.get_output()

# -----------------------
# User Selects ONE Room
# -----------------------
selection = uidoc.Selection.GetElementIds()
if len(selection) != 1:
    forms.alert("Please select a single room.", title="Assign Room Number", warn_icon=True)
    script.exit()

selected_room = doc.GetElement(list(selection)[0])
if not isinstance(selected_room, SpatialElement) or selected_room.Category.Id.IntegerValue != int(BuiltInCategory.OST_Rooms):
    forms.alert("Selected element is not a room.", title="Assign Room Number", warn_icon=True)
    script.exit()


# -----------------------
# Step 1: Ask for LL (Level Code)
# -----------------------
level_options = ["B3U", "B3", "B3T", "B2", "B1", "L1", "L2", "L2M", "L3", "L4", "L5", "L6", "L6M", "ROF", "Other"]
user_level = forms.SelectFromList.show(level_options, title="Select Level Code", button_name="Select", multiselect=False)

if not user_level:
    script.exit()

if user_level == "Other":
    user_level = forms.ask_for_string(prompt="Enter custom Level Code (LL):", title="Custom Level Code")
    if not user_level:
        script.exit()


# -----------------------
# Step 2: Get SSSS (Sector) and F (Function) from Room Parameters
# -----------------------
def get_param_value(element, param_name):
    param = element.LookupParameter(param_name)
    if param and param.HasValue:
        return param.AsString() or ""
    return ""

sector = get_param_value(selected_room, "SECTOR").strip()
function = get_param_value(selected_room, "ROOM FUNCTION").strip()

if not sector or not function:
    forms.alert("Missing 'SECTOR' or 'ROOM FUNCTION' parameter on the selected room.", title="Missing Parameters", warn_icon=True)
    script.exit()


# -----------------------
# Step 3: Get existing Room Numbers with same LL-SSSS-F
# -----------------------
def get_all_room_numbers(documents):
    room_numbers = set()
    pattern = r"^{0}-{1}-{2}(\d{{2}})$".format(user_level, sector, function)
    regex = Regex(pattern)

    for d in documents:
        collector = FilteredElementCollector(d).OfCategory(BuiltInCategory.OST_Rooms).WhereElementIsNotElementType()
        for room in collector:
            if room.Id == selected_room.Id and d == doc:
                continue  # Skip the selected room
            number = get_param_value(room, "Number")
            if number:
                match = regex.Match(number)
                if match.Success:
                    room_numbers.add(int(match.Groups[1].Value))
    return room_numbers



# Include linked documents
linked_docs = []
for link in FilteredElementCollector(doc).OfClass(RevitLinkInstance):
    try:
        linked_doc = link.GetLinkDocument()
        if linked_doc:
            linked_docs.append(linked_doc)
    except:
        continue

all_docs = [doc] + linked_docs
used_numbers = get_all_room_numbers(all_docs)


# -----------------------
# Step 4: Find available NN (01-99)
# -----------------------
available_nn = None
for i in range(1, 100):
    if i not in used_numbers:
        available_nn = "{:02}".format(i)
        break

if not available_nn:
    forms.alert("No available numbers (01-99) for this LL-SSSS-F combination.", title="Number Full", warn_icon=True)
    script.exit()

# -----------------------
# Step 5: Combine & Update Room Number
# -----------------------
new_number = "{0}-{1}-{2}{3}".format(user_level, sector, function, available_nn)

t = Transaction(doc, "Assign Room Number")
t.Start()
selected_room.LookupParameter("Number").Set(new_number)
t.Commit()


