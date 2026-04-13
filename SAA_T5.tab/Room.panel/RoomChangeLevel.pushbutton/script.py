# -*- coding: utf-8 -*-
__title__ = "Change\nLevel"
__author__ = "JK_Sim"
__doc__ = """Version = 2.1
Date    = 04.12.2025
Description:
Changes level via Grouping + TransactionGroup.
Fix: Re-finds the group by Type ID in Step 2 to handle Revit ID changes.
"""

from pyrevit import revit, DB, forms
from pyrevit.forms import SelectFromList
from Autodesk.Revit.DB import (
    Transaction, TransactionGroup, FilteredElementCollector, BuiltInParameter,
    ElementId, FailureProcessingResult, IFailuresPreprocessor, 
    BuiltInCategory, Group, SpatialElement
)
from System.Collections.Generic import List
import sys

# ---------------- Failure preprocessor ----------------
class AutoOKAllWarnings(IFailuresPreprocessor):
    def PreprocessFailures(self, fa):
        try:
            for fm in fa.GetFailureMessages():
                if fm.GetSeverity() == DB.FailureSeverity.Warning:
                    fa.DeleteWarning(fm)
        except:
            pass
        return FailureProcessingResult.Continue

# ---------------- Helpers ----------------
def get_level_id(element):
    if isinstance(element, DB.Architecture.Room):
        p = element.get_Parameter(BuiltInParameter.ROOM_LEVEL_ID)
        if p: return p.AsElementId()
    elif isinstance(element, DB.ModelLine):
        return element.LevelId
    return ElementId.InvalidElementId

# ---------------- MAIN ----------------
doc = __revit__.ActiveUIDocument.Document
uidoc = __revit__.ActiveUIDocument

# 1. Filter Selection
sel_ids = uidoc.Selection.GetElementIds()
if not sel_ids:
    sys.exit()

valid_elements = []
valid_ids = List[ElementId]()

for i in sel_ids:
    e = doc.GetElement(i)
    if e.Category.Id.IntegerValue == int(BuiltInCategory.OST_Rooms) \
    or e.Category.Id.IntegerValue == int(BuiltInCategory.OST_RoomSeparationLines):
        valid_elements.append(e)
        valid_ids.Add(i)

if not valid_elements:
    sys.exit()

# 2. VALIDATION
first_elem_level_id = get_level_id(valid_elements[0])
is_consistent = True
for e in valid_elements:
    if get_level_id(e) != first_elem_level_id:
        is_consistent = False
        break

if not is_consistent:
    forms.alert(
        "Selected elements are on different levels.\nPlease select elements on the SAME level.",
        title="Level Mismatch",
        exitscript=True
    )

# 3. Select Target Level
all_levels = FilteredElementCollector(doc).OfClass(DB.Level).ToElements()
all_levels = sorted(all_levels, key=lambda x: x.Elevation)

class LevelOption(object):
    def __init__(self, level):
        self.level = level
        self.name = level.Name
    def __repr__(self):
        return self.name

level_options = [LevelOption(l) for l in all_levels]
selected_option = SelectFromList.show(level_options, title="Select Target Level", button_name="Move")

if not selected_option:
    sys.exit()

target_level_id = selected_option.level.Id

# ---------------- MULTI-STEP TRANSACTION ----------------
tg = TransactionGroup(doc, "Change Room Level")
tg.Start()

try:
    # Variables to pass data between transactions
    target_group_type_id = None

    # --- TRANSACTION 1: GROUP & MOVE ---
    t1 = Transaction(doc, "Step 1: Group and Move")
    t1.Start()
    
    fh = t1.GetFailureHandlingOptions()
    fh.SetFailuresPreprocessor(AutoOKAllWarnings())
    t1.SetFailureHandlingOptions(fh)

    group = doc.Create.NewGroup(valid_ids)
    
    if group:
        # Save the Type ID immediately. The Type ID is stable; the Instance ID is not.
        target_group_type_id = group.GroupType.Id
        
        # Move
        try:
            group.LevelId = target_level_id
        except:
            p = group.get_Parameter(BuiltInParameter.GROUP_LEVEL)
            if p: p.Set(target_level_id)
        
        doc.Regenerate()

    t1.Commit()

    # --- TRANSACTION 2: UNGROUP & CLEANUP ---
    # We only proceed if we successfully captured the Type ID in T1
    if target_group_type_id:
        t2 = Transaction(doc, "Step 2: Ungroup")
        t2.Start()
        
        fh2 = t2.GetFailureHandlingOptions()
        fh2.SetFailuresPreprocessor(AutoOKAllWarnings())
        t2.SetFailureHandlingOptions(fh2)

        # RE-FIND STRATEGY:
        # Do not look for the old Group ID. Look for the Group Instance that uses our Group Type.
        found_group = None
        group_instances = FilteredElementCollector(doc).OfClass(Group).ToElements()
        
        for g in group_instances:
            if g.GroupType.Id == target_group_type_id:
                found_group = g
                break
        
        if found_group:
            found_group.UngroupMembers()
            doc.Delete(target_group_type_id)
        
        t2.Commit()

    tg.Assimilate()

except Exception as e:
    tg.RollBack()
    forms.alert("An error occurred:\n{}".format(e))